import os
import torch
import argparse
import tqdm
from datetime import datetime
from dataloader import build_hmdb51_loader, build_kinetics_loader
from model import *
from torch.utils import data
from torchsummary import summary
from utils.train_utils import accuracy_metric, AvgMeter, summary_graph
from torch.utils.tensorboard import SummaryWriter

model_zoo = {
    "conv3d": Conv3D,
    "conv2d_lstm": Conv2DLSTM,
    "slowfast": SlowFast,
}

parser = argparse.ArgumentParser(description="Performance test")

parser.add_argument("mod", choices=["train", "eval", "summary"])
# data loader
parser.add_argument("--fpc", type=int, default=32, help="frame per clip")
parser.add_argument("-W", "--width", type=int, default=112)
parser.add_argument("-H", "--height", type=int, default=112)
#   dataset
dataset_parser = parser.add_subparsers(help="chose a dataset to load", title="dataset")
hmdb_parser = dataset_parser.add_parser("hmdb51")
hmdb_parser.set_defaults(dataset="hmdb51")
hmdb_parser.add_argument("--video", type=str, help="hmdb51 video file", required=True)
hmdb_parser.add_argument("--annotation", type=str, help="hmdb51 split file", required=True)
kinetics_parser = dataset_parser.add_parser("kinetics")
kinetics_parser.set_defaults(dataset="kinetics")
kinetics_parser.add_argument("--video", type=str, help="kinetics dataset video folder", required=True)
# train
parser.add_argument("--batch_size", "--bs", default=4, type=int)
parser.add_argument("--split_train", "--st", default=1, type=int,
                    help="(for low GPU memory) do optimize after [split_train] train step")
parser.add_argument("--epoch", default=10, type=int)
parser.add_argument("--check_freq", default=1, type=int, help="for every N epoch, run eval and save checkpoint")
parser.add_argument("--lr", default=0.001, type=float)
# log
parser.add_argument("--log", "-l", default=None, type=str, required=True,
                    help="directory to save tensorboard log and checkpoint")
parser.add_argument("--model", type=str, choices=[k for k, v in model_zoo.items()], default="conv3d")
parser.add_argument("--resume", type=str, default=None, help="resume from checkpoint")
parser.add_argument("--timestamp", type=str, default=None, help="naming log folder")
# lr schedule
parser.add_argument("--lr_schedule", type=tuple, default=(5, 8), help="decay at epoch")
parser.add_argument("--lr_decay_rate", type=float, default=0.1, help="lr=lr*decay_rate")


def main():
    args = parser.parse_args()
    timestamp = args.timestamp if args.timestamp is not None else "{0:%Y-%m-%dT%H-%M-%SW}".format(datetime.now())
    log_dir = os.path.join(args.log, timestamp)
    ckp_dir = os.path.join(log_dir, "checkpoint")
    os.makedirs(ckp_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # net
    net: torch.nn.Module = model_zoo[args.model]()
    # train
    writer = SummaryWriter(log_dir=log_dir)
    criterion = torch.nn.CrossEntropyLoss()

    if args.mod == "summary":
        summary(net, (3, args.fpc, args.height, args.width), device="cpu")
        # tensorboard graph
        summary_graph((1, 3, args.fpc, args.height, args.width), net, writer)
    else:
        # optimizer
        optimizer = torch.optim.Adam(net.parameters(), lr=args.lr)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.9)

        if args.resume is not None:
            state_dict = torch.load(args.resume)
            epoch_start = state_dict["epoch"]
            assert state_dict["arch"] == args.model, "The model architecture is not match"
            net.load_state_dict(state_dict["model_param"])
            optimizer.load_state_dict(state_dict["optimizer"])
            scheduler.load_state_dict(state_dict["scheduler"])
        else:
            epoch_start = 0

        # load train, eval and test data
        if args.dataset == "hmdb51":
            dataloader_train = build_hmdb51_loader(args.video, args.annotation, args.batch_size,
                                                   train=True,
                                                   size=(args.height, args.width),
                                                   frame_per_clip=args.fpc)
            dataloader_test = dataloader_val = build_hmdb51_loader(args.video, args.annotation,
                                                                   args.batch_size,
                                                                   train=False,
                                                                   size=(args.height, args.width),
                                                                   frame_per_clip=args.fpc)
        elif args.dataset == "kinetics":
            dataloader_train = build_kinetics_loader(os.path.join(args.video, "train"),
                                                     args.batch_size,
                                                     size=(args.height, args.width),
                                                     train=True,
                                                     frame_per_clip=args.fpc)
            dataloader_val = build_kinetics_loader(os.path.join(args.video, "val"),
                                                   args.batch_size,
                                                   size=(args.height, args.width),
                                                   train=False,
                                                   frame_per_clip=args.fpc)
            dataloader_test = build_kinetics_loader(os.path.join(args.video, "test"),
                                                    args.batch_size,
                                                    size=(args.height, args.width),
                                                    train=False,
                                                    frame_per_clip=args.fpc)
        else:
            raise ValueError
        dataloader_train: data.DataLoader
        dataloader_val: data.DataLoader
        dataloader_test: data.DataLoader

        if args.mod == "train":

            for epoch in range(epoch_start, args.epoch):
                train(dataloader_train, net, optimizer, criterion, accuracy_metric, epoch,
                      writer=writer, split=args.split_trin)

                if (epoch + 1) % 1 == 0:
                    # save
                    ckp_file = "checkpoint_{}.pt".format(epoch + 1)
                    ckp_path = os.path.join(ckp_dir, ckp_file)
                    state_dict = {
                        "epoch": epoch + 1,
                        "arch": args.model,
                        "model_param": net.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "scheduler": scheduler.state_dict()
                    }
                    torch.save(state_dict, ckp_path)
                    # eval
                    loss, top1, top5 = eval(dataloader_val, net, criterion, accuracy_metric)
                    writer.add_scalars("eval/acc", {"top1": top1, "top5": top5}, global_step=epoch + 1)
                    writer.add_scalar("eval/loss", loss, global_step=epoch + 1)
        else:
            eval(dataloader_test, net, criterion, accuracy_metric)
    writer.close()


def train(data_loader: data.DataLoader, net: torch.nn.Module, optimizer: torch.optim.Optimizer,
          criterion: torch.nn.Module, acc_metric, epoch, writer=None, split=1):
    top1 = AvgMeter("Acc@1", ":4.2f")
    top5 = AvgMeter("Acc@5", ":4.2f")

    net.cuda()
    net.train()
    optimizer.zero_grad()
    data_loader = tqdm.tqdm(data_loader)
    data_loader.set_description("Train")
    for step, (video, audio, label) in enumerate(data_loader):
        video = video.cuda()
        label = label.cuda()

        logits = net(video)
        loss = criterion(logits, label)
        loss.backward()

        if step % split == 0:
            optimizer.step()
            optimizer.zero_grad()

        acc1, acc5 = acc_metric(logits, label, topk=(1, 5))
        top1.update(acc1, video.size(0))
        top5.update(acc5, video.size(0))

        if writer is not None:
            total_step = step + epoch * len(data_loader)
            writer.add_scalars("train/acc", {"top1": top1.val, "top5": top5.val}, total_step)
            writer.add_scalar("train/loss", loss, total_step)


def eval(data_loader: data.DataLoader, net: torch.nn.Module, criterion, acc_metric):
    top1 = AvgMeter("Acc@1", ":4.2f")
    top5 = AvgMeter("Acc@5", ":4.2f")
    avg_loss = AvgMeter("Loss", ":3.4f")

    net.cuda()
    net.eval()
    data_loader = tqdm.tqdm(data_loader)
    data_loader.set_description("Eval")
    with torch.no_grad():
        for step, (video, audio, label) in enumerate(data_loader):
            video = video.cuda()
            label = label.cuda()

            logits = net(video)

            loss = criterion(logits, label)
            acc1, acc5 = acc_metric(logits, label, (1, 5))

            avg_loss.update(loss, video.size(0))
            top1.update(acc1, video.size(0))
            top5.update(acc5, video.size(0))
            data_loader.set_postfix_str(str(top1) + " " + str(top5))
    return avg_loss.avg, top1.avg, top5.avg


if __name__ == '__main__':
    main()
