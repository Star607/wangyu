from .models import TGraphSAGE, write_result
import argparse
from datetime import datetime
import itertools
import logging
import os
import sys
import time

import dgl  # import dgl after torch will cause `GLIBCXX_3.4.22` not found.
from dgl.nn.pytorch.conv import SAGEConv
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.nn import functional as F
from tqdm import trange
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.utils import resample

from data_loader.data_util import load_data
from model.utils import get_free_gpu, timeit, EarlyStopMonitor
from torch_model.util_dgl import set_logger, construct_dglgraph
from torch_model.layers import TemporalNodeLayer, TSAGEConv
from torch_model.eid_precomputation import _prepare_deg_indices, _par_deg_indices, _deg_indices_full, _par_deg_indices_full, _latest_edge, LatestNodeInteractionFinder
from .models import TemporalLinkTrainer
# A cpp extension computing upper_bound along the last dimension of an non-decreasing matrix. It saves huge memory use.
import upper_bound_cpp

# Change the order so that it is the one used by "nvidia-smi" and not the
# one used by all other programs ("FASTEST_FIRST")
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"


class LR(torch.nn.Module):
    def __init__(self, dim, drop=0.1):
        super().__init__()
        self.fc_1 = torch.nn.Linear(dim, 80)
        self.fc_2 = torch.nn.Linear(80, 10)
        self.fc_3 = torch.nn.Linear(10, 1)
        self.act = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(p=drop, inplace=True)

    def forward(self, x):
        x = self.act(self.fc_1(x))
        x = self.dropout(x)
        x = self.act(self.fc_2(x))
        x = self.dropout(x)
        return self.fc_3(x).squeeze(dim=1)


def prepare_node_dataset(dataset):
    edges, nodes = load_data(dataset=dataset, mode="format")[0]

    id2idx = {row.node_id: row.id_map for row in nodes.itertuples()}
    edges["from_node_id"] = edges["from_node_id"].map(id2idx)
    edges["to_node_id"] = edges["to_node_id"].map(id2idx)

    tmax, tmin = edges["timestamp"].max(), edges["timestamp"].min()
    edges["timestamp"] = (edges["timestamp"] - tmin) / (tmax - tmin + 1e-7)
    val_time, test_time = list(np.quantile(edges["timestamp"], [0.70, 0.85]))
    train_data = edges[edges["timestamp"] <= val_time]
    valid_data = edges[(edges["timestamp"] > val_time) &
                       (edges["timestamp"] <= test_time)]
    test_data = edges[edges["timestamp"] > test_time]
    return nodes, edges, train_data, valid_data, test_data


def stratified_batch(train_ids, labels, nbatch):
    vals = list(np.unique(labels))
    vids = [train_ids[labels == v] for v in vals]  # edge_ids for each value
    vsize = [len(ids) // nbatch for ids in vids]  # batch_size for each value
    for size, val in zip(vsize, vals):
        assert size > 0, "Value {} is less than batch numbers.".format(val)
    for idx in range(nbatch):
        batch_ids = [ids[idx * vsize[i]: (idx + 1) * vsize[i]]
                     for i, ids in enumerate(vids)]
        yield np.concatenate(batch_ids)


def balance_batch(train_ids, labels, nbatch, neg_ratio=1):
    pos_ids = train_ids[labels == 1]
    neg_ids = train_ids[labels == 0]
    pos_ids = pos_ids.repeat(len(neg_ids) // (len(pos_ids) * neg_ratio))
    train_ids = np.concatenate([pos_ids, neg_ids])
    labels = np.concatenate([np.ones(len(pos_ids)), np.zeros(len(neg_ids))])
    return stratified_batch(train_ids, labels, nbatch)


def eval_nodeclass(embeds, lr_model, eids, val_data, batch_size=None):
    if batch_size is None:
        batch_size = val_data.shape[0]
    lr_model.eval()
    val_data = val_data.iloc[:batch_size]
    with torch.no_grad():
        batch_embeds = embeds[eids]
        logits = lr_model(batch_embeds).sigmoid().cpu().numpy()
        labels = val_data["state_label"].to_numpy()
        acc = accuracy_score(labels, logits >= 0.5)
        f1 = f1_score(labels, logits >= 0.5)
        auc = roc_auc_score(labels, logits)
    return acc, f1, auc


def main(args, logger):
    logger.info(args)

    # Set device utility.
    if args.gpu:
        if args.gid >= 0:
            device = torch.device("cuda:{}".format(args.gid))
        else:
            device = torch.device("cuda:{}".format(get_free_gpu()))
        logger.info("Begin Conv on Device %s, GPU Memory %d GB", device,
                    torch.cuda.get_device_properties(device).total_memory // 2**30)
    else:
        device = torch.device("cpu")

    # Load nodes, edges, and labeled dataset for training, validation and test.
    nodes, edges, train_data, val_data, test_data = prepare_node_dataset(
        args.dataset)
    logger.info("Train, valid, test: %d, %d, %d", (train_data["state_label"] == 1).sum(),
                (val_data["state_label"] == 1).sum(), (test_data["state_label"] == 1).sum())
    delta = edges["timestamp"].shift(-1) - edges["timestamp"]
    # Pandas loc[low:high] includes high, so we use slice operations here instead.
    assert np.all(delta[:len(delta) - 1] >= 0)

    # Set DGLGraph, node_features, edge_features, and edge timestamps.
    g = construct_dglgraph(edges, nodes, device, bidirected=args.bidirected)
    if not args.trainable:
        g.ndata["nfeat"] = torch.zeros_like(g.ndata["nfeat"])
    deg_indices = _par_deg_indices_full(g)
    for k, v in deg_indices.items():
        g.edata[k] = v.to(device).unsqueeze(-1).detach()

    # Set model configuration.
    # Input features: node_featurs + edge_features + time_encoding
    in_feats = (g.ndata["nfeat"].shape[-1] + g.edata["efeat"].shape[-1])
    tgcl = TemporalLinkTrainer(g, in_feats, args.n_hidden, args.n_hidden, args)
    tgcl = tgcl.to(device)

    logger.info("loading saved TGCL model")
    gcn_lr = '%.4f' % args.gcn_lr
    lam = '%.1f' % args.lam
    margin = '%.1f' % args.margin
    model_path = f"./saved_models/{args.dataset}-{args.agg_type}-{gcn_lr}-{lam}-{margin}.pth"
    tgcl.load_state_dict(torch.load(model_path))
    tgcl.eval()
    logger.info("TGCL models loaded")
    logger.info("Start training node classification task")

    lr_model = LR(args.n_hidden * 2).to(device)
    optimizer = torch.optim.Adam(
        lr_model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    train_ids = np.arange(len(train_data))
    val_eids = len(train_data) + np.arange(len(val_data))
    test_eids = len(train_data) + len(val_data) + np.arange(len(test_data))
    batch_size = args.batch_size
    num_batch = np.int(np.ceil(len(train_data) / batch_size))
    epoch_bar = trange(args.epochs, disable=(not args.display))
    early_stopper = EarlyStopMonitor(max_round=5)
    loss_fn = nn.BCEWithLogitsLoss()
    start = time.time()
    with torch.no_grad():
        g.ndata["deg"] = (g.in_degrees() +
                          g.out_degrees()).to(g.ndata["nfeat"])
        src_feat, dst_feat = tgcl.conv(g)
        embeds = torch.cat((src_feat, dst_feat), dim=1)
    logger.info("Convolution takes %.2f secs.", time.time() - start)

    for epoch in epoch_bar:
        np.random.shuffle(train_ids)
        batch_bar = trange(num_batch, disable=(not args.display))
        batch_sampler = balance_batch(
            train_ids, train_data.loc[train_ids, "state_label"], num_batch)
        for idx in batch_bar:
            tgcl.eval()
            lr_model.train()

            # batch_ids = next(batch_sampler)
            batch_ids = resample(
                train_ids, n_samples=batch_size, stratify=train_data["state_label"])
            # batch_ids = train_ids[idx * batch_size: (idx + 1) * batch_size]
            node_ids = train_data.loc[batch_ids, "from_node_id"].to_numpy()
            ts = train_data.loc[batch_ids, "timestamp"].to_numpy()
            labels = train_data.loc[batch_ids, "state_label"].to_numpy()

            optimizer.zero_grad()
            batch_embeds = embeds[batch_ids]
            lr_prob = lr_model(batch_embeds)
            loss = loss_fn(lr_prob, torch.tensor(labels).to(lr_prob))
            loss.backward()
            optimizer.step()

            acc, f1, auc = eval_nodeclass(embeds, lr_model, val_eids, val_data)
            batch_bar.set_postfix(loss=loss.item(), acc=acc, f1=f1, auc=auc)
        acc, f1, auc = eval_nodeclass(embeds, lr_model, val_eids, val_data)
        epoch_bar.update()
        epoch_bar.set_postfix(loss=loss.item(), acc=acc, f1=f1, auc=auc)

        def ckpt_path(
            epoch): return f'./nc-ckpt/{args.lr}-{args.batch_size}-{epoch}.pth'
        if early_stopper.early_stop_check(auc):
            logger.info('No improvment over {} epochs, stop training'.format(
                early_stopper.max_round))
            logger.info(
                f'Loading the best model at epoch {early_stopper.best_epoch}')
            best_model_path = ckpt_path(early_stopper.best_epoch)
            lr_model.load_state_dict(torch.load(best_model_path))
            logger.info(
                f'Loaded the best model at epoch {early_stopper.best_epoch} for inference')
            break
        else:
            torch.save(lr_model.state_dict(), ckpt_path(epoch))

    lr_model.eval()
    _, _, val_auc = eval_nodeclass(embeds, lr_model, val_eids, val_data)
    acc, f1, auc = eval_nodeclass(embeds, lr_model, test_eids, test_data)
    params = {"best_epoch": early_stopper.best_epoch,
              "bidirected": args.bidirected,
              "batch_size": args.batch_size,
              "sampling": args.sampling,
              "neg_ratio": args.neg_ratio}
    write_result(val_auc, (acc, f1, auc), args.dataset,
                 params, postfix="NC-GTC")


def edge_args(parser):
    parser.add_argument("--norm", action="store_true")
    parser.add_argument("--trainable",
                        dest="trainable", action="store_true")
    parser.add_argument("--time-encoding", "-te", type=str, default="cosine",
                        help="Time encoding function.", choices=["concat", "cosine", "outer"])
    parser.add_argument("--n-hidden", type=int, default=128,
                        help="number of hidden gcn units")
    parser.add_argument("--n-layers", type=int, default=2,
                        help="number of hidden gcn layers")
    parser.add_argument("--n-neg", type=int, default=1,
                        help="number of negative samples")
    parser.add_argument("--pos-contra", "-pc", action="store_true")
    parser.add_argument("--neg-contra", '-nc', action="store_true")
    parser.add_argument("--lam", type=float, default=0.0,
                        help="Weight for contrastive loss.")
    parser.add_argument("--remain-history", "-rh",
                        "-hist", action="store_true")
    parser.add_argument("--n-hist", type=int, default=1,
                        help="number of history samples")
    parser.add_argument("--margin", type=float, default=0.1)
    parser.add_argument("--weight-decay", type=float, default=1e-5,
                        help="Weight for L2 loss")
    parser.add_argument("--agg-type", type=str, default="gcn",
                        help="Aggregator type: mean/gcn/pool")
    return parser


def parse_args():
    # trainable, n_layers, dropout, agg_type, time_encoding, n_neg, n_hist, pos_contra, neg_contra, remain_history, lam, norm
    parser = argparse.ArgumentParser(description='Temporal GraphSAGE')
    parser.add_argument("-d", "--dataset", type=str, default="JODIE-wikipedia",
                        choices=["JODIE-wikipedia", "JODIE-mooc", "JODIE-reddit"])
    parser.add_argument("--bidirected", dest="bidirected", action="store_true",
                        help="For non-bipartite graphs, set this as True.")
    parser.add_argument("--dropout", type=float, default=0.2,
                        help="dropout probability")
    parser.add_argument("--log-file", action="store_true")
    parser.add_argument("--gpu", dest="gpu", action="store_true",
                        help="Whether use GPU.")
    parser.add_argument("--no-gpu", dest="gpu", action="store_false",
                        help="Whether use GPU.")
    parser.add_argument("--gid", type=int, default=-1,
                        help="Specify GPU id.")
    parser.add_argument("--lr", type=float, default=1e-4,
                        help="learning rate")
    parser.add_argument("--epochs", type=int, default=50,
                        help="number of training epochs")
    parser.add_argument("-bs", "--batch-size", type=int, default=30)
    parser.add_argument("--sampling", default="normal",
                        choices=["normal", "resample", "balance"])
    parser.add_argument("--neg-ratio", type=int, default=1)
    parser.add_argument("--gcn-lr", type=float, default=0.01)
    parser.add_argument("--display", dest="display", action="store_true")
    parser.add_argument("--no-display", dest="display", action="store_false"
                        )
    parser = edge_args(parser)
    return parser


if __name__ == "__main__":
    def warn(*args, **kwargs):
        pass
    import warnings
    warnings.warn = warn
    parser = parse_args()
    args = parser.parse_args()
    logger = set_logger()
    main(args, logger)
