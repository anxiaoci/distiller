"""
This file defines the core research contribution   
"""
import os
import torch
from torch.nn import functional as F
import torch.nn as nn
import torchvision
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from argparse import ArgumentParser
import torch.optim as optim
import pytorch_lightning as pl
import numpy as np
from collections import OrderedDict
from research_seed.baselines.random_baseline.dataset import RandomCifarDataset
import torchvision
import torchvision.transforms as transforms

def str2bool(v):
	if v.lower() in ('yes', 'true', 't', 'y', '1'):
		return True
	else:
		return False

def load_model_chk(model, path):
    chkp = torch.load(path)
    new_state_dict = OrderedDict()
    for k, v in chkp['state_dict'].items():
        name = k[6:] # remove `model.`
        new_state_dict[name] = v
    model.load_state_dict(new_state_dict)
    return model

class Random_Cifar(pl.LightningModule):

    def __init__(self, student, teacher, hparams):
        super(Random_Cifar, self).__init__()
        # not the best model...
        self.hparams = hparams

        self.student = student
        self.teacher = teacher

        # Loading from checkpoint
        self.teacher = load_model_chk(self.teacher, hparams.path_to_teacher)

        self.teacher.eval()
        self.student.train()

        # self.criterion = nn.CrossEntropyLoss()

        self.train_step = 0
        self.train_num_correct = 0

        self.val_step = 0
        self.val_num_correct = 0

        self.mse_loss = nn.MSELoss()


    def loss_fn_kd(self, outputs, teacher_outputs):
        """
        Credits: https://github.com/peterliht/knowledge-distillation-pytorch/blob/e4c40132fed5a45e39a6ef7a77b15e5d389186f8/model/net.py#L100

        Compute the knowledge-distillation (KD) loss given outputs, labels.
        "Hyperparameters": temperature and alpha
        NOTE: the KL Divergence for PyTorch comparing the softmaxs of teacher
        and student expects the input tensor to be log probabilities! See Issue #2
        """
        
        alpha = self.hparams.alpha
        T = self.hparams.temperature

        # mse loss between output of feature map of student and teacher. 
        loss = self.mse_loss(outputs, teacher_outputs)

        return loss

    def forward(self, x, mode):
        if mode == 'student':
            return self.student(x, True)
        elif mode == 'teacher':
            return self.teacher(x, True)
        else:
            raise ValueError("mode should be teacher or student")

    def training_step(self, batch, batch_idx):

        x, y = batch

        y_teacher = self.forward(x, 'teacher')
        y_student = self.forward(x, 'student')
        
        loss = self.loss_fn_kd(y_student, y_teacher)

        return {
            'loss': loss,
            'log' : {
                'train_loss' : loss.item()
            } 
        }


    def validation_step(self, batch, batch_idx):
        self.student.eval()
        x, y = batch


        # y_hat = self.forward(x, 'student')
        # val_loss = self.criterion(y_hat, y)

        # pred = y_hat.data.max(1, keepdim=True)[1]

        # self.val_step += x.size(0)
        # self.val_num_correct += pred.eq(y.data.view_as(pred)).cpu().sum()
        y_teacher = self.forward(x, 'teacher')
        y_student = self.forward(x, 'student')

        val_loss = self.loss_fn_kd(y_student, y_teacher)

        return {
            'val_loss': val_loss
        }

    def validation_end(self, outputs):
        # OPTIONAL
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        
        log_metrics = {
            'val_avg_loss': avg_loss.item(),
            # 'val_accuracy': float(self.val_num_correct*100/self.val_step)
        }

        self.scheduler.step(np.around(avg_loss.item(),2))

        # reset logging stuff
        self.train_step = 0
        self.train_num_correct = 0
        self.val_step = 0
        self.val_num_correct = 0

        # back to training
        self.student.train()

        return {'val_loss': avg_loss, 'log': log_metrics}

    def configure_optimizers(self):
        # REQUIRED
        # can return multiple optimizers and learning_rate schedulers
        if self.hparams.optim == 'adam':
            optimizer = torch.optim.Adam(self.student.parameters(), lr=self.hparams.learning_rate)
        elif self.hparams.optim == 'sgd':
            optimizer = torch.optim.SGD(self.student.parameters(), nesterov=True, momentum=self.hparams.momentum, 
            weight_decay=self.hparams.weight_decay, lr=self.hparams.learning_rate)
        else:
            raise ValueError('No such optimizer, please use adam or sgd')
 
        self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min',patience=5,factor=0.5,verbose=True)
        return optimizer

    @pl.data_loader
    def train_dataloader(self):

        transform_test = transforms.Compose([
                        transforms.Resize((32, 32)),
                        transforms.ToTensor(),
                        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
                    ])

        trainset = torchvision.datasets.STL10('./data', split='train+unlabeled', 
        folds=None, transform=transform_test, target_transform=None, download=True)

        if self.hparams.gpus > 1:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(trainset)
        else:
            dist_sampler = None

        return DataLoader(trainset, batch_size=self.hparams.batch_size, num_workers=self.hparams.num_workers, sampler=dist_sampler)

    @pl.data_loader
    def val_dataloader(self):
        
        transform_test = transforms.Compose([
                        transforms.Resize((32, 32)),
                        transforms.ToTensor(),
                        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
                    ])

        valset = torchvision.datasets.STL10('./data', split='test', 
        folds=None, transform=transform_test, target_transform=None, download=True)

        if self.hparams.gpus > 1:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(valset)
        else:
            dist_sampler = None

        return DataLoader(valset, batch_size=self.hparams.batch_size, num_workers=self.hparams.num_workers, sampler=dist_sampler)

    @pl.data_loader
    def test_dataloader(self):
        
        transform_test = transforms.Compose([
                        transforms.Resize((32, 32)),
                        transforms.ToTensor(),
                        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
                    ])

        testset = torchvision.datasets.STL10('./data', split='test', 
        folds=None, transform=transform_test, target_transform=None, download=True)

        if self.hparams.gpus > 1:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(testset)
        else:
            dist_sampler = None

        return DataLoader(testset, batch_size=self.hparams.batch_size, num_workers=self.hparams.num_workers, sampler=dist_sampler)


    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        parser = ArgumentParser(parents=[parent_parser])
        parser.add_argument('--dataset', default='cifar10', type=str, help='dataset. can be either cifar10 or cifar100')
        parser.add_argument('--batch-size', default=128, type=int, help='batch_size')
        parser.add_argument('--learning-rate', default=0.001, type=float, help='initial learning rate')
        parser.add_argument('--momentum', default=0.9, type=float,  help='SGD momentum')
        parser.add_argument('--weight-decay', default=1e-4, type=float, help='SGD weight decay (default: 1e-4)')
        parser.add_argument('--dataset-dir', default='./data', type=str,  help='dataset directory')
        parser.add_argument('--optim', default='adam', type=str, help='Optimizer')
        parser.add_argument('--num-workers', default=4, type=float,  help='Num workers for data loader')
        parser.add_argument('--student-model', default='resnet8', type=str, help='teacher student name')
        parser.add_argument('--teacher-model', default='resnet110', type=str, help='teacher student name')
        parser.add_argument('--path-to-teacher', default='', type=str, help='teacher chkp path')
        parser.add_argument('--temperature', default=10, type=float, help='Temperature for knowledge distillation')
        parser.add_argument('--alpha', default=0.7, type=float, help='Alpha for knowledge distillation')
        return parser

