import os

import torch
from torch import nn
from torchvision import transforms
from data.anime_data import WaterMarkDataset, PairDatasetMark
from argparse import ArgumentParser
from loguru import logger
import datetime
from models import NAFNet, Discriminator
from PIL import Image

from accelerate import Accelerator
from accelerate.utils import set_seed
from utils import cal_psnr

class Trainer:
    def __init__(self, args):
        self.args=args
        self.alpha = args.alpha

        set_seed(42)

        self.accelerator = Accelerator(
            gradient_accumulation_steps=1,
            step_scheduler_with_optimizer=False,
        )

        self.build_data()
        self.build_model()

        self.local_rank = int(os.environ.get("LOCAL_RANK", -1))
        self.world_size = self.accelerator.num_processes

        if self.accelerator.is_local_main_process:
            logname = os.path.join(args.log_dir, datetime.datetime.now().isoformat() + '.log')
            logger.add(logname)
            logger.info(f'world size: {self.world_size}')
        else:
            logger.disable("__main__")

        self.net_G, self.net_D, self.optimizer, train_loader, val_loader = \
            self.accelerator.prepare(self.net_G, self.net_D, self.optimizer, self.train_loader, self.test_loader)

    def build_model(self):
        self.net_G = NAFNet(width=24, enc_blk_nums=[1,2,4,6], middle_blk_num=8, dec_blk_nums=[2,2,1,1])
        self.net_D = Discriminator((3,800,800))

        #summary(self.net_G, (3, 224, 224))

        self.optimizer = torch.optim.AdamW(self.net_G.parameters(), lr=self.args.lr)
        self.optimizer_D = torch.optim.AdamW(self.net_D.parameters(), lr=self.args.lr)
        self.criterion = nn.SmoothL1Loss()
        self.criterion_gan = nn.MSELoss()
        self.criterion_mask = nn.SmoothL1Loss(reduction='none')
        print(len(self.train_loader))

        # self.scheduler = lr_scheduler.OneCycleLR(self.optimizer, max_lr=self.args.lr,
        #                                     steps_per_epoch=len(self.train_loader), epochs=self.args.epochs,
        #                                     pct_start=0.2)
        # self.scheduler_D = lr_scheduler.OneCycleLR(self.optimizer_D, max_lr=self.args.lr,
        #                                     steps_per_epoch=len(self.train_loader), epochs=self.args.epochs,
        #                                     pct_start=0.2)

    def build_data(self):
        water_mark = Image.open(self.args.water_mark)
        water_mark_mask = Image.open(self.args.water_mark_mask).convert('RGB')
        self.data_train = PairDatasetMark(root_clean=self.args.train_root_clean, root_mark=self.args.train_root_mark,
                                          water_mark=water_mark, water_mark_mask=water_mark_mask,
                                          transform=transforms.Compose([
                                                transforms.Resize(800),
                                                transforms.CenterCrop(800),
                                                transforms.ToTensor(),
                                                transforms.Normalize([0.5], [0.5]),
                                           ]),)
        self.data_test = WaterMarkDataset(root=self.args.test_root, water_mark=water_mark, water_mark_mask=water_mark_mask,
                                          noise_std=0,
                                          transform=transforms.Compose([
                                              transforms.Resize(800),
                                              transforms.CenterCrop(800),
                                              transforms.ToTensor(),
                                              transforms.Normalize([0.5], [0.5]),
                                          ]),)

        self.train_loader = torch.utils.data.DataLoader(self.data_train, batch_size=self.args.bs, shuffle=True,
                                                        num_workers=self.args.num_workers, pin_memory=True)
        self.test_loader = torch.utils.data.DataLoader(self.data_test, batch_size=self.args.bs, shuffle=False,
                                                        num_workers=self.args.num_workers, pin_memory=True)

    def train(self):
        valid = torch.tensor(1.).to(self.accelerator.device)
        fake = torch.tensor(0.).to(self.accelerator.device)

        loss_sum_G, loss_sum_D = 0, 0
        for ep in range(self.args.epochs):
            for step, (img, img_clean, fake_mark) in enumerate(self.train_loader):
                img = img.to(self.accelerator.device)
                img_clean = img_clean.to(self.accelerator.device)
                fake_mark = fake_mark.to(self.accelerator.device)

                #  Train Generators
                self.net_G.train()
                self.net_G.requires_grad_(True)
                self.net_D.eval()
                self.net_D.requires_grad_(False)
                self.optimizer.zero_grad()

                fake_A = self.net_G(img)
                fake_B = self.net_G(fake_mark)
                pred_fake_A = self.net_D(fake_A)
                loss = self.criterion_gan(pred_fake_A, valid) + self.alpha*self.criterion(img_clean, fake_B)

                self.accelerator.backward(loss)
                self.optimizer.step()
                #self.scheduler.step()

                loss_sum_G += loss.item()

                #  Train Discriminator
                self.net_G.eval()
                self.net_G.requires_grad_(False)
                self.net_D.train()
                self.net_D.requires_grad_(True)
                self.optimizer_D.zero_grad()

                pred_real = self.net_D(img_clean)
                pred_img_fake = self.net_D(img)
                pred_fake = self.net_D(fake_A.detach())

                loss = (self.criterion_gan(pred_real, valid) + self.criterion_gan(pred_fake, fake) + self.criterion_gan(pred_img_fake, fake))/2

                self.accelerator.backward(loss)
                self.optimizer_D.step()
                #self.scheduler_D.step()

                loss_sum_D += loss.item()

                if step % self.args.log_step == 0:
                    logger.info(f'[{ep+1}/{self.args.epochs}]<{step+1}/{len(self.train_loader)}>, '
                                f'loss_G:{loss_sum_G / self.args.log_step:.3e}, '
                                f'loss_D:{loss_sum_D / self.args.log_step:.3e}, '
                                f'lr:{self.optimizer.state_dict()["param_groups"][0]["lr"]:.3e}')
                    loss_sum_G = 0
                    loss_sum_D = 0
            self.test()
            if self.accelerator.is_local_main_process:
                torch.save(self.net_G.state_dict(), f'output_GAN/ep_{ep}.pth')

    @torch.no_grad()
    def test(self):
        self.net_G.eval()
        mean = torch.tensor([0.5]).to(self.accelerator.device)
        std = torch.tensor([0.5]).to(self.accelerator.device)
        psnr=0
        for step, (img, img_clean, img_mask) in enumerate(self.test_loader):
            img = img.to(self.accelerator.device)
            img_clean = img_clean.to(self.accelerator.device)

            pred = self.net_G(img)

            psnr+=cal_psnr(pred, img_clean, mean, std).sum().item()

        psnr = torch.tensor(psnr).to(self.accelerator.device)
        psnr = self.accelerator.reduce(psnr, reduction="sum")

        logger.info(f'psnr: {psnr/len(self.data_test):.3f}')

def make_args():
    parser = ArgumentParser()
    parser.add_argument("--train_root_clean", default='../datas/clean_imgs', type=str)
    parser.add_argument("--train_root_mark", default='../datas/imgs_water_mark', type=str)
    parser.add_argument("--test_root", default='../datas/anime_SR/test/HR', type=str)
    parser.add_argument("--water_mark", default='./water_mark2.png', type=str)
    parser.add_argument("--water_mark_mask", default='./water_mark2_mask.png', type=str)
    parser.add_argument("--bs", default=4, type=int)
    parser.add_argument("--lr", default=1e-3, type=float)
    parser.add_argument("--epochs", default=100, type=int)
    parser.add_argument("--num_workers", default=8, type=int)
    parser.add_argument("--log_dir", default='logs/', type=str)
    parser.add_argument("--log_step", default=20, type=int)

    parser.add_argument("--alpha", default=0.1, type=float)
    args = parser.parse_args()
    return args

if __name__ == '__main__':
    args = make_args()
    os.makedirs('./output_GAN', exist_ok=True)
    os.makedirs(args.log_dir, exist_ok=True)

    trainer = Trainer(args)
    trainer.train()