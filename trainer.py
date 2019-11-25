import json
import os
import torch
import torch.nn.functional as F
from torch import nn
from tqdm import tqdm
from optimizer import get_optimizer, get_scheduler


def load_checkpoint(model, checkpoint_path):
    model_ckp = torch.load(checkpoint_path)
    model.load_state_dict(model_ckp["model_state_dict"])
    return model


def init_progress_bar(train_loader):
    batch_size = train_loader.batch_size
    bar_format = "{desc}{percentage:3.0f}%"
    # bar_format += "|{bar}|"
    bar_format += " {n_fmt}/{total_fmt} [{elapsed} < {remaining}]"
    bar_format += "{postfix}"
    t = tqdm(total=len(train_loader) * batch_size, bar_format=bar_format)
    return t


def check_dir(directory):
    # create the folder if it does not exit
    if not directory == "" and not os.path.exists(directory):
        print(f"Folder {directory} does not exist! Creating...")
        os.makedirs(directory)


class Trainer():
    def __init__(self, net, config):
        self.net = net
        self.device = config["device"]
        self.name = config["test_name"]

        # Retrieve preconfigured optimizers and schedulers for all runs
        optim_cls, optim_args = get_optimizer(config["optim"], config)
        sched_cls, sched_args = get_scheduler(config["sched"], config)
        self.optimizer = optim_cls(net.parameters(), **optim_args)
        self.scheduler = sched_cls(self.optimizer, **sched_args)
        self.loss_fun = nn.CrossEntropyLoss()
        self.train_loader = config["train_loader"]
        self.test_loader = config["test_loader"]
        self.batch_size = self.train_loader.batch_size
        self.config = config
        # tqdm bar
        self.t_bar = None
        folder = config["results_dir"]
        check_dir(folder)
        self.best_model_file = folder.joinpath(f"{self.name}_best.pth.tar")
        acc_file_name = folder.joinpath(f"{self.name}.csv")
        self.acc_file = acc_file_name.open("w+")
        self.acc_file.write("Training Loss,Validation Loss\n")
        conf_file_name = folder.joinpath(f"{self.name}_conf.json")
        with conf_file_name.open("w+") as conf:
            json.dump(config, conf, indent=4, sort_keys=True,
                      default=lambda o: "obj")

    def set_optimizer(self, optimizer):
        self.optimizer = optimizer

    def set_scheduler(self, scheduler):
        self.scheduler = scheduler

    def set_loss_fun(self, loss_fun):
        self.loss_fun = loss_fun

    def calculate_loss(self, data, target):
        raise NotImplementedError(
            "calculate_loss should be implemented by subclass!")

    def train_single_epoch(self, t_bar):
        self.net.train()
        total_loss = 0
        for batch_idx, (data, target) in enumerate(self.train_loader):
            data = data.to(self.device)
            target = target.to(self.device)
            self.optimizer.zero_grad()
            loss = self.calculate_loss(data, target)
            total_loss += loss
            t_bar.update(len(data))
            loss_avg = total_loss / batch_idx
            t_bar.set_postfix_str(f"Loss {loss_avg:.6f}")
        return total_loss / len(self.train_loader.dataset)

    def train(self):
        epochs = self.config["epochs"]

        best_acc = 0
        t_bar = init_progress_bar(self.train_loader)
        for epoch in range(epochs):
            # update progress bar
            t_bar.reset()
            t_bar.set_description(f"Epoch {epoch}")
            # perform training
            train_acc = self.train_single_epoch(t_bar)
            # validate the output and save if it is the best so far
            val_acc = self.validate()
            if val_acc > best_acc:
                best_acc = val_acc
                self.save(epoch, name=self.best_model_file)
            # update the scheduler
            if self.scheduler:
                self.scheduler.step()
            self.acc_file.write(f"{train_acc},{val_acc}\n")
        tqdm.clear(t_bar)
        t_bar.close()
        self.acc_file.close()
        return best_acc

    def validate(self):
        self.net.eval()
        with torch.no_grad():
            correct = 0
            acc = 0
            for images, labels in self.test_loader:
                images = images.to(self.device)
                labels = labels.to(self.device)
                output = self.net(images)
                # Standard Learning Loss ( Classification Loss)
                loss = self.loss_fun(output, labels)
                # get the index of the max log-probability
                pred = output.data.max(1, keepdim=True)[1]
                correct += pred.eq(labels.data.view_as(pred)).cpu().sum()

            acc = float(correct) / len(self.test_loader.dataset)
            print(f"\nValidation set: Average loss: {loss:.4f}, "
                  f"Accuracy: {correct}/{len(self.test_loader.dataset)} "
                  f"({acc * 100.0:.3f}%)\n")
            return acc

    def save(self, epoch, name):
        torch.save({
            "model_state_dict": self.net.state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "epoch": epoch,
        }, name)


class BaseTrainer(Trainer):

    def calculate_loss(self, data, target):
        # Standard Learning Loss ( Classification Loss)
        output = self.net(data)
        loss = self.loss_fun(output, target)
        loss.backward()
        self.optimizer.step()
        return loss


class KDTrainer(Trainer):
    def __init__(self, s_net, t_net, config):
        super(KDTrainer, self).__init__(s_net, config)
        # the student net is the base net
        self.s_net = self.net
        self.t_net = t_net
        # set the teacher net into evaluation mode
        self.t_net.eval()
        self.t_net.train(mode=False)

    def calculate_loss(self, data, target):
        lambda_ = self.config["lambda_student"]
        T = self.config["T_student"]
        output = self.s_net(data)

        # Standard Learning Loss ( Classification Loss)
        loss = self.loss_fun(output, target)

        # Knowledge Distillation Loss
        teacher_outputs = self.t_net(data)
        student_max = F.log_softmax(output / T, dim=1)
        teacher_max = F.softmax(teacher_outputs / T, dim=1)
        loss_KD = nn.KLDivLoss(reduction="batchmean")(student_max, teacher_max)
        loss = (1 - lambda_) * loss + lambda_ * T * T * loss_KD
        loss.backward()
        self.optimizer.step()
        return loss