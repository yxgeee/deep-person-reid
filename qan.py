from __future__ import print_function, absolute_import
import os
import sys
import time
import datetime
import argparse
import os.path as osp
import numpy as np

fileDir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, fileDir)

import torch
import torch.nn as nn
from torch.nn import functional as F
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler

import dataset.data_manager as data_manager
from dataset.dataset_loader import ImageDataset, VideoDataset
import util.transforms as T
# import models
from util.losses import CrossEntropyLabelSmooth, TripletLoss
from util.utils import AverageMeter, Logger, save_checkpoint
from util.eval_metrics import evaluate
from util.samplers import RandomIdentitySampler
# from optimizers import init_optim
from models.QualityNet import qualitynet_res50

parser = argparse.ArgumentParser(description='Train video model with cross entropy loss')
# Datasets
parser.add_argument('--root', type=str, default='data', help="root path to data directory")
parser.add_argument('-d', '--dataset', type=str, default='ilidsvid',
                    choices=data_manager.get_names())
parser.add_argument('-j', '--workers', default=4, type=int,
                    help="number of data loading workers (default: 4)")
parser.add_argument('--height', type=int, default=256,
                    help="height of an image (default: 256)")
parser.add_argument('--width', type=int, default=128,
                    help="width of an image (default: 128)")
parser.add_argument('--seq-len', type=int, default=15, help="number of images to sample in a tracklet")
# Optimization options
parser.add_argument('--optim', type=str, default='adam', help="optimization algorithm (see optimizers.py)")
parser.add_argument('--max-epoch', default=500, type=int,
                    help="maximum epochs to run")
parser.add_argument('--start-epoch', default=0, type=int,
                    help="manual epoch number (useful on restarts)")
parser.add_argument('-b', '--train-batch', default=16, type=int,
                    help="train batch size")
parser.add_argument('--test-batch', default=4, type=int, help="test batch size (number of tracklets)")
parser.add_argument('--lr', '--learning-rate', default=0.0003, type=float,
                    help="initial learning rate")
parser.add_argument('--stepsize', default=200, type=int,
                    help="stepsize to decay learning rate (>0 means this is enabled)")
parser.add_argument('--gamma', default=0.1, type=float,
                    help="learning rate decay")
parser.add_argument('--weight-decay', default=5e-04, type=float,
                    help="weight decay (default: 5e-04)")
parser.add_argument('--margin', type=float, default=0.3, help="margin for triplet loss")
parser.add_argument('--num-instances', type=int, default=4,
                    help="number of instances per identity")
parser.add_argument('--htri-only', action='store_true', default=False,
                    help="if this is True, only htri loss is used in training")
# Architecture
# parser.add_argument('-a', '--arch', type=str, default='resnet50', choices=models.get_names())
parser.add_argument('--pool', type=str, default='qan', choices=['avg', 'max', 'qan'])
# Miscs
parser.add_argument('--print-freq', type=int, default=1, help="print frequency")
parser.add_argument('--seed', type=int, default=1, help="manual seed")
parser.add_argument('--resume', type=str, default='', metavar='PATH')
parser.add_argument('--evaluate', action='store_true', help="evaluation only")
parser.add_argument('--eval-step', type=int, default=50,
                    help="run evaluation for every N epochs (set to -1 to test after training)")
parser.add_argument('--save-dir', type=str, default='log')
parser.add_argument('--use-cpu', action='store_true', help="use cpu")
parser.add_argument('--gpu-devices', default='0', type=str, help='gpu device ids for CUDA_VISIBLE_DEVICES')

args = parser.parse_args()

def init_optim(optim, params, lr, weight_decay):
    if optim == 'adam':
        return torch.optim.Adam(params, lr=lr, weight_decay=weight_decay)
    elif optim == 'sgd':
        return torch.optim.SGD(params, lr=lr, momentum=0.9, weight_decay=weight_decay)
    elif optim == 'rmsprop':
        return torch.optim.RMSprop(params, lr=lr, momentum=0.9, weight_decay=weight_decay)
    else:
        raise KeyError("Unsupported optim: {}".format(optim))

def main():
    torch.manual_seed(args.seed)
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_devices
    use_gpu = torch.cuda.is_available()
    if args.use_cpu: use_gpu = False

    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_train.txt'))
    else:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_test.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    if use_gpu:
        print("Currently using GPU {}".format(args.gpu_devices))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(args.seed)
    else:
        print("Currently using CPU (GPU is highly recommended)")

    print("Initializing dataset {}".format(args.dataset))
    dataset = data_manager.init_dataset(root=args.root, name=args.dataset)

    transform_train = T.Compose([
        T.Random2DTranslation(args.height, args.width),
        # T.RectScale(args.height, args.width),
        T.RandomSizedEarser(),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    transform_test = T.Compose([
        T.Resize((args.height, args.width)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    pin_memory = True if use_gpu else False

    # decompose tracklets into images for image-based training
    new_train = []
    for img_paths, pid, camid in dataset.train:
        for img_path in img_paths:
            new_train.append((img_path, pid, camid))

    trainloader = DataLoader(
        VideoDataset(dataset.train, seq_len=args.seq_len, sample='random', transform=transform_train), shuffle=True,
        # ImageDataset(new_train, transform=transform_train),
        # sampler=RandomIdentitySampler(new_train, num_instances=args.num_instances),
        batch_size=args.train_batch, num_workers=args.workers,
        pin_memory=pin_memory, drop_last=True,
    )

    queryloader = DataLoader(
        VideoDataset(dataset.query, seq_len=args.seq_len, sample='evenly', transform=transform_test),
        batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
        pin_memory=pin_memory, drop_last=False,
    )

    galleryloader = DataLoader(
        VideoDataset(dataset.gallery, seq_len=args.seq_len, sample='evenly', transform=transform_test),
        batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
        pin_memory=pin_memory, drop_last=False,
    )

    # print("Initializing model: {}".format(args.arch))
    # model = models.init_model(name=args.arch, num_classes=dataset.num_train_pids, loss={'xent', 'htri'})
    model = qualitynet_res50(dataset.num_train_pids, loss={'xent', 'htri'}, seq_len=args.seq_len)
    print("Model size: {:.5f}M".format(sum(p.numel() for p in model.parameters())/1000000.0))

    criterion_xent = CrossEntropyLabelSmooth(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
    criterion_htri = TripletLoss(margin=args.margin)

    param_groups = [{'params': model.base_model.parameters(), 'lr_mult': 1.0},
                    {'params': model.feature_model.parameters(), 'lr_mult': 10.0},
                    {'params': model.quality_model.parameters(), 'lr_mult': 10.0}]
    optimizer = init_optim(args.optim, param_groups, args.lr, args.weight_decay)
    if args.stepsize > 0:
        scheduler = lr_scheduler.StepLR(optimizer, step_size=args.stepsize, gamma=args.gamma)
    start_epoch = args.start_epoch

    if args.resume:
        print("Loading checkpoint from '{}'".format(args.resume))
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        start_epoch = checkpoint['epoch']

    if use_gpu:
        model = nn.DataParallel(model).cuda()

    if args.evaluate:
        print("Evaluate only")
        test(model, queryloader, galleryloader, args.pool, use_gpu)
        return

    start_time = time.time()
    train_time = 0
    best_rank1 = -np.inf
    best_epoch = 0
    print("==> Start training")

    for epoch in range(start_epoch, args.max_epoch):
        start_train_time = time.time()
        train(epoch, model, criterion_xent, criterion_htri, optimizer, trainloader, use_gpu)
        train_time += round(time.time() - start_train_time)

        if args.stepsize > 0: scheduler.step()

        if args.eval_step > 0 and (epoch+1) % args.eval_step == 0 or (epoch+1) == args.max_epoch:
            print("==> Test")
            rank1 = test(model, queryloader, galleryloader, args.pool, use_gpu)
            is_best = rank1 > best_rank1
            if is_best:
                best_rank1 = rank1
                best_epoch = epoch + 1

            if use_gpu:
                state_dict = model.module.state_dict()
            else:
                state_dict = model.state_dict()
            save_checkpoint({
                'state_dict': state_dict,
                'rank1': rank1,
                'epoch': epoch,
            }, is_best, osp.join(args.save_dir, 'checkpoint_ep' + str(epoch+1) + '.pth.tar'))

    print("==> Best Rank-1 {:.1%}, achieved at epoch {}".format(best_rank1, best_epoch))

    elapsed = round(time.time() - start_time)
    elapsed = str(datetime.timedelta(seconds=elapsed))
    train_time = str(datetime.timedelta(seconds=train_time))
    print("Finished. Total elapsed time (h:m:s): {}. Training time (h:m:s): {}.".format(elapsed, train_time))

def train(epoch, model, criterion_xent, criterion_htri, optimizer, trainloader, use_gpu):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses_img = AverageMeter()
    losses_seq = AverageMeter()
    xent_img = AverageMeter()
    htri_img = AverageMeter()
    xent_seq = AverageMeter()
    htri_seq = AverageMeter()
    model.train()

    end = time.time()
    for batch_idx, (imgs, pids, _) in enumerate(trainloader):
        data_time.update(time.time() - end)
        batch_size = pids.size(0)
        labels = pids
        if len(imgs.shape)>4:
            imgs = imgs.view(-1, 3, args.height, args.width)
            pids = pids.view(batch_size,1).expand(batch_size, args.seq_len).contiguous().view(-1)
        if use_gpu:
            imgs, pids, labels = imgs.cuda(), pids.cuda(), labels.cuda()
        outputs, features, scores = model(imgs)
        scores = scores.view(-1, args.seq_len)
        scores = F.softmax(scores, dim=1)
        scores = scores.view(-1, 1)

        if args.htri_only:
            # only use hard triplet loss to train the network
            loss_img = criterion_htri(features, pids)
        else:
            # combine hard triplet loss with cross entropy loss
            xent_loss = criterion_xent(outputs, pids)
            htri_loss = criterion_htri(features, pids)
            xent_img.update(xent_loss.item(), pids.size(0))
            htri_img.update(htri_loss.item(), pids.size(0))
            loss_img = xent_loss + htri_loss

        # outputs = outputs.view(batch_size, args.seq_len, -1)
        features = features.view(batch_size, args.seq_len, -1)
        scores = scores.view(batch_size, args.seq_len, -1).expand(batch_size, args.seq_len, features.size(-1))

        features = (features * scores).sum(1)
        scores = scores.sum(1)
        features = features / scores
        seq_out, _ = model(features)
        if args.htri_only:
            # only use hard triplet loss to train the network
            loss_seq = criterion_htri(features, labels)
        else:
            # combine hard triplet loss with cross entropy loss
            xent_loss = criterion_xent(seq_out, labels)
            htri_loss = criterion_htri(features, labels)
            xent_seq.update(xent_loss.item(), labels.size(0))
            htri_seq.update(htri_loss.item(), labels.size(0))
            loss_seq = xent_loss + htri_loss
        loss = loss_img + loss_seq

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        losses_img.update(loss_img.item(), pids.size(0))
        losses_seq.update(loss_seq.item(), labels.size(0))

        batch_time.update(time.time() - end)
        end = time.time()

        if (batch_idx+1) % args.print_freq == 0:
            # print("Epoch {}/{}\t Batch {}/{}\t Loss {:.6f} ({:.6f})".format(
            #     epoch+1, args.max_epoch, batch_idx+1, len(trainloader), losses.val, losses.avg
            # ))
            if args.htri_only:
                print('Epoch: [{0}/{1}][{2}/{3}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                      'ImgLoss {img_loss.val:.6f} ({img_loss.avg:.6f})\t'
                      'SeqLoss {seq_loss.val:.6f} ({seq_loss.avg:.6f})'.format(
                       epoch+1, args.max_epoch, batch_idx+1, len(trainloader), batch_time=batch_time,
                       data_time=data_time, img_loss=losses_img, seq_loss=losses_seq))
            else:
                print('Epoch: [{0}/{1}][{2}/{3}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                      'ImgLoss {img_loss.val:.6f} ({img_loss.avg:.6f})\t'
                      'ImgXent {xent_img.val:.6f} ({xent_img.avg:.6f})\t'
                      'ImgHtri {htri_img.val:.6f} ({htri_img.avg:.6f})\t'
                      'SeqLoss {seq_loss.val:.6f} ({seq_loss.avg:.6f})\t'
                      'SeqXent {xent_seq.val:.6f} ({xent_seq.avg:.6f})\t'
                      'SeqHtri {htri_seq.val:.6f} ({htri_seq.avg:.6f})'.format(
                       epoch+1, args.max_epoch, batch_idx+1, len(trainloader), batch_time=batch_time,
                       data_time=data_time, img_loss=losses_img, xent_img=xent_img, 
                       htri_img=htri_img, seq_loss=losses_seq, xent_seq=xent_seq, htri_seq=htri_seq))


def test(model, queryloader, galleryloader, pool, use_gpu, ranks=[1, 5, 10, 20]):
    model.eval()

    with torch.no_grad():
        qf, q_pids, q_camids = [], [], []
        for batch_idx, (imgs, pids, camids) in enumerate(queryloader):
            if use_gpu: imgs = imgs.cuda()
            b, s, c, h, w = imgs.size()
            imgs = imgs.view(b*s, c, h, w)
            _, features, scores = model(imgs)
            scores = scores.view(-1, args.seq_len)
            scores = F.softmax(scores, dim=1)
            scores = scores.view(-1, 1)
            features = features.view(b, s, -1)
            if pool == 'avg':
                features = torch.mean(features, 1)
            elif pool == 'qan':
                scores = scores.view(b, s, -1).expand(b, s, features.size(-1))
                features = (features * scores).sum(1)
                scores = scores.sum(1)
                features = features / scores
            else:
                features, _ = torch.max(features, 1)
            features = features.data.cpu()
            qf.append(features)
            q_pids.extend(pids)
            q_camids.extend(camids)
        qf = torch.cat(qf, 0)
        q_pids = np.asarray(q_pids)
        q_camids = np.asarray(q_camids)

        print("Extracted features for query set, obtained {}-by-{} matrix".format(qf.size(0), qf.size(1)))

        gf, g_pids, g_camids = [], [], []
        for batch_idx, (imgs, pids, camids) in enumerate(galleryloader):
            if use_gpu: imgs = imgs.cuda()
            b, s, c, h, w = imgs.size()
            imgs = imgs.view(b*s, c, h, w)
            _, features, scores = model(imgs)
            scores = scores.view(-1, args.seq_len)
            scores = F.softmax(scores, dim=1)
            scores = scores.view(-1, 1)
            features = features.view(b, s, -1)
            if pool == 'avg':
                features = torch.mean(features, 1)
            elif pool == 'qan':
                scores = scores.view(b, s, -1).expand(b, s, features.size(-1))
                features = (features * scores).sum(1)
                scores = scores.sum(1)
                features = features / scores
            else:
                features, _ = torch.max(features, 1)
            features = features.data.cpu()
            gf.append(features)
            g_pids.extend(pids)
            g_camids.extend(camids)
        gf = torch.cat(gf, 0)
        g_pids = np.asarray(g_pids)
        g_camids = np.asarray(g_camids)

        print("Extracted features for gallery set, obtained {}-by-{} matrix".format(gf.size(0), gf.size(1)))

    print("Computing distance matrix")

    m, n = qf.size(0), gf.size(0)
    distmat = torch.pow(qf, 2).sum(dim=1, keepdim=True).expand(m, n) + \
              torch.pow(gf, 2).sum(dim=1, keepdim=True).expand(n, m).t()
    distmat.addmm_(1, -2, qf, gf.t())
    distmat = distmat.numpy()

    print("Computing CMC and mAP")
    cmc, mAP = evaluate(distmat, q_pids, g_pids, q_camids, g_camids)

    print("Results ----------")
    print("mAP: {:.1%}".format(mAP))
    print("CMC curve")
    for r in ranks:
        print("Rank-{:<3}: {:.1%}".format(r, cmc[r-1]))
    print("------------------")

    return cmc[0]

if __name__ == '__main__':
    main()
