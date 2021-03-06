
# coding: utf-8

from os import path as osp
import numpy as np
import tensorflow as tf
import sonnet as snt
from attrdict import AttrDict

from evaluation import make_fig, make_logger

from data import load_data, tensors_from_data
from mnist_model import AIRonMNIST


# In[ ]:

learning_rate = 1e-4
batch_size = 64
n_steps = 3

results_dir = '../results'
run_name = 'multi_mnist'

logdir = osp.join(results_dir, run_name)
checkpoint_name = osp.join(logdir, 'model.ckpt')
axes = {'imgs': 0, 'labels': 0, 'nums': 1}


# In[ ]:

use_prior = True

num_steps_prior = AttrDict(
    anneal='exp',
    init=1. - 1e-7,
    final=1e-5,
    steps_div=1e4,
    steps=1e5
)

appearance_prior = AttrDict(loc=0., scale=1.)
where_scale_prior = AttrDict(loc=.5, scale=1.)
where_shift_prior = AttrDict(scale=1.)

use_reinforce = True
sample_presence = True
step_bias = 1.
transform_var_bias = -3.

init_explore_eps = .00

l2_weight = 0.


# In[ ]:

valid_data = load_data('mnist_validation.pickle')
train_data = load_data('mnist_train.pickle')


# In[ ]:

tf.reset_default_graph()
train_tensors = tensors_from_data(train_data, batch_size, axes, shuffle=True)
valid_tensors = tensors_from_data(valid_data, batch_size, axes, shuffle=False)
x, valid_x = train_tensors['imgs'], valid_tensors['imgs']
y, valid_y = train_tensors['nums'], valid_tensors['nums']
    
n_hidden = 32 * 8
n_layers = 2
n_hiddens = [n_hidden] * n_layers
    
air = AIRonMNIST(x, y,
                max_steps=n_steps, 
                explore_eps=init_explore_eps,
                inpt_encoder_hidden=n_hiddens,
                glimpse_encoder_hidden=n_hiddens,
                glimpse_decoder_hidden=n_hiddens,
                transform_estimator_hidden=n_hiddens,
                steps_pred_hidden=[128, 64],
                baseline_hidden=[256, 128],
                transform_var_bias=transform_var_bias,
                step_bias=step_bias)


# In[ ]:

train_step, global_step = air.train_step(learning_rate, l2_weight, appearance_prior, where_scale_prior,
                            where_shift_prior, num_steps_prior)


# In[ ]:

config = tf.ConfigProto()
config.gpu_options.allow_growth = True
    
sess = tf.Session(config=config)
sess.run(tf.global_variables_initializer())
all_summaries = tf.summary.merge_all()


# In[ ]:

summary_writer = tf.summary.FileWriter(logdir, sess.graph)
saver = tf.train.Saver(max_to_keep=100)

# In[ ]:

train_batches = train_data['imgs'].shape[0]
valid_batches = valid_data['imgs'].shape[0]
log = make_logger(air, sess, summary_writer, train_tensors, train_batches, valid_tensors, valid_batches)


# In[ ]:

train_itr = sess.run(global_step)
print 'Starting training at iter = {}'.format(train_itr)

if train_itr == 0:
    log(0)

while train_itr <= 300 * 1e3:
        
    train_itr, _ = sess.run([global_step, train_step])
    
    if train_itr % 1000 == 0:
        summaries = sess.run(all_summaries)
        summary_writer.add_summary(summaries, train_itr)
        
    if train_itr % 10000 == 0:
        log(train_itr)
        
    if train_itr % 10000 == 0:
        saver.save(sess, checkpoint_name, global_step=train_itr)
        make_fig(air, sess, logdir, train_itr)    
