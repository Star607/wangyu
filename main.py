import os
import time
import numpy as np
import tensorflow.compat.v1 as tf

from data_loader.minibatch import load_data, TruncatedTemporalEdgeBatchIterator
from data_loader.neigh_samplers import MaskNeighborSampler, TemporalNeighborSampler
from model.gta import GraphTemporalAttention, SAGEInfo

# from tensorflow.python.util import deprecation
# deprecation._PRINT_DEPRECATION_WARNINGS = False
tf.logging.set_verbosity(tf.logging.ERROR)

flags = tf.app.flags
FLAGS = flags.FLAGS

flags.DEFINE_string('dataset', 'CTDNE-fb-forum', 'experiment dataset')
flags.DEFINE_float('learning_rate', 0.00001, 'initial learning rate.')
flags.DEFINE_integer('epochs', 1, 'number of epochs to train.')
flags.DEFINE_float('dropout', 0.0, 'dropout rate (1 - keep probability).')
flags.DEFINE_float('weight_decay', 0.0,
                   'weight for l2 loss on embedding matrix.')

flags.DEFINE_integer('max_degree', 100, 'maximum node degree.')
flags.DEFINE_integer('samples_1', 25, 'number of samples in layer 1')
flags.DEFINE_integer('samples_2', 10, 'number of users samples in layer 2')
flags.DEFINE_integer(
    'dim_1', 128, 'Size of output dim (final is 2x this, if using concat)')
flags.DEFINE_integer(
    'dim_2', 128, 'Size of output dim (final is 2x this, if using concat)')
flags.DEFINE_integer('neg_sample_size', 20, 'number of negative samples')
flags.DEFINE_integer('context_size', 20, 'number of temporal context samples')
flags.DEFINE_integer('batch_size', 128, 'minibatch size.')
flags.DEFINE_boolean(
    'concat', False, 'Use concat between neighbor features and self features.')

# logging, saving, validation settings etc.
flags.DEFINE_boolean('save_embeddings', True,
                     'whether to save embeddings for all nodes after training')
flags.DEFINE_string('base_log_dir', '.',
                    'base directory for logging and saving embeddings')
flags.DEFINE_integer('validate_iter', 5000,
                     "how often to run a validation minibatch.")
flags.DEFINE_integer('validate_batch_size', 256,
                     "how many nodes per validation sample.")
flags.DEFINE_integer('gpu', 0, "which gpu to use.")
flags.DEFINE_integer('print_every', 50, "How often to print training info.")
flags.DEFINE_integer('max_total_steps', 10**10,
                     "Maximum total number of iterations")
os.environ["CUDA_VISIBLE_DEVICES"] = str(FLAGS.gpu)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


def log_dir():
    log_dir = FLAGS.base_log_dir + "/unsup-" + \
        FLAGS.dataset
    log_dir += "/{model:s}_{model_size:s}_{lr:0.6f}/".format(
        model=FLAGS.model,
        model_size=FLAGS.model_size,
        lr=FLAGS.learning_rate)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    return log_dir

# Define model evaluation function


def evaluate(sess, model, minibatch_iter, size=None):
    t_test = time.time()
    feed_dict_val = minibatch_iter.val_feed_dict(size)
    outs_val = sess.run([model.loss, model.ranks, model.mrr],
                        feed_dict=feed_dict_val)
    return outs_val[0], outs_val[1], outs_val[2], (time.time() - t_test)


def save_val_embeddings(sess, model, minibatch_iter, size, out_dir, mod=""):
    val_embeddings = []
    finished = False
    seen = set([])
    nodes = []
    iter_num = 0
    name = "val"
    while not finished:
        feed_dict_val, finished, edges = minibatch_iter.incremental_embed_feed_dict(
            size, iter_num)
        iter_num += 1
        outs_val = sess.run([model.loss, model.mrr, model.outputs1],
                            feed_dict=feed_dict_val)
        # ONLY SAVE FOR embeds1 because of planetoid
        for i, edge in enumerate(edges):
            if not edge[0] in seen:
                val_embeddings.append(outs_val[-1][i, :])
                nodes.append(edge[0])
                seen.add(edge[0])
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    val_embeddings = np.vstack(val_embeddings)
    np.save(out_dir + name + mod + ".npy",  val_embeddings)
    with open(out_dir + name + mod + ".txt", "w") as fp:
        fp.write("\n".join(map(str, nodes)))


def incremental_evaluate(sess, model, minibatch_iter, size, test=False):
    t_test = time.time()
    finished = False
    val_losses = []
    val_mrrs = []
    iter_num = 0
    while not finished:
        feed_dict_val, finished, _ = minibatch_iter.incremental_val_feed_dict(
            size, iter_num)
        iter_num += 1
        outs_val = sess.run([model.loss, model.ranks, model.mrr],
                            feed_dict=feed_dict_val)
        val_losses.append(outs_val[0])
        val_mrrs.append(outs_val[2])
    return np.mean(val_losses), np.mean(val_mrrs), (time.time() - t_test)


def construct_placeholders():
    # Define placeholders
    placeholders = {
        "batch_from": tf.placeholder(tf.int32, shape=(None), name="batch_from"),
        "batch_to": tf.placeholder(tf.int32, shape=(None), name="batch_to"),
        "timestamp": tf.placeholder(tf.float64, shape=(None), name="timestamp"),
        "batch_size": tf.placeholder(tf.int32, name="batch_size"),
        "context_from": tf.placeholder(tf.int32, shape=(None), name="context_from"),
        "context_to": tf.placeholder(tf.int32, shape=(None), name="context_to"),
        "context_timestamp": tf.placeholder(tf.float64, shape=(None), name="timestamp"),
    }
    return placeholders


def train(edges, nodes):
    placeholders = construct_placeholders()
    batch = TruncatedTemporalEdgeBatchIterator(
        edges, nodes, placeholders, batch_size=FLAGS.batch_size, max_degree=FLAGS.max_degree)
    sampler = MaskNeighborSampler(batch.adj_ids, batch.adj_tss)
    # layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1),
                #    SAGEInfo("node", sampler, FLAGS.samples_2, FLAGS.dim_2)]

    layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1)]
    model = GraphTemporalAttention(
        placeholders, None,
        (batch.adj_ids, batch.adj_tss), batch.deg, layer_infos=layer_infos, context_layer_infos=layer_infos, batch_size=FLAGS.batch_size, learning_rate=FLAGS.learning_rate,
        concat=FLAGS.concat, embed_dim=128)

    config = tf.ConfigProto(log_device_placement=FLAGS.log_device_placement)
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True

    # Initialize session
    sess = tf.Session(config=config)
    merged = tf.summary.merge_all()
    summary_writer = tf.summary.FileWriter(log_dir(), sess.graph)

    # Init variables
    sess.run(tf.global_variables_initializer())

    total_steps = 0
    avg_time = 0.0
    epoch_val_costs = []
    for epoch in range(FLAGS.epochs):
        batch.shuffle()
        itr = 0
        print("Epoch %04d" % (epoch+1))
        epoch_val_costs.append(0)
        while not batch.end():
            feed_dict = batch.next_train_batch()
            t = time.time()
            outs = sess.run([merged, model.opt_op, model.loss, model.ranks,
                             model.aff_all, model.mrr, model.output_from], feed_dict)
            train_cost = outs[2]
            train_mrr = outs[5]
            if itr % FLAGS.validate_itr == 0:
                val_cost, ranks, val_mrr, duration = evaluate(
                    sess, model, batch, size=FLAGS.validate_batch_size)
                epoch_val_costs[-1] += val_cost
            if total_steps % FLAGS.print_every == 0:
                summary_writer.add_summary(outs[0], total_steps)
            avg_time = (avg_time * total_steps +
                        time.time() - t) / (total_steps + 1)
            if total_steps % FLAGS.print_every == 0:
                print("Iter:", '%04d' % itr,
                      "train_loss=", "{:.5f}".format(train_cost),
                      "train_mrr=", "{:.5f}".format(train_mrr),
                      "val_loss=", "{:.5f}".format(val_cost),
                      "val_mrr=", "{:.5f}".format(val_mrr),
                      "time=", "{:.5f}".format(avg_time))

            itr += 1
            total_steps += 1
            if total_steps > FLAGS.total_steps:
                break
        if total_steps > FLAGS.total_steps:
            break
    print("Optimization Finishied!")
    val_cost, val_f1_mac, duration = incremental_evaluate(
        sess, model, batch, FLAGS.batch_size)
    print("Full validation stats:",
          "loss=", "{:.5f}".format(val_cost),
          "f1_macro=", "{:.5f}".format(val_f1_mac),
          "time=", "{:.5f}".format(duration))
    with open(log_dir() + "val_stats.txt", "w") as fp:
        fp.write("loss={:.5f} f1_macro={:.5f} time={:.5f}".
                 format(val_cost, val_f1_mac, duration))

    print("Writing test set stats to file (don't peak!)")
    val_cost, val_f1_mac, duration = incremental_evaluate(
        sess, model, batch, FLAGS.batch_size, test=True)
    with open(log_dir() + "test_stats.txt", "w") as fp:
        fp.write("loss={:.5f} f1_macro={:.5f}".
                 format(val_cost, val_f1_mac))


def main(argv=None):
    print("Loading training data...")
    edges, nodes = load_data(dataset=FLAGS.dataset)
    print("Done loading training data...")
    train(edges, nodes)


if __name__ == "__main__":
    tf.app.run()