import numpy as  np
import sonnet as snt
import tensorflow as tf
from tensorflow.contrib.distributions import Bernoulli, NormalWithSoftplusScale

from modules import SpatialTransformer, ParametrisedGaussian
from neural import Affine


class AIRCell(snt.RNNCore):
    """RNN cell that implements the core features of Attend, Infer, Repeat, as described here:
    https://arxiv.org/abs/1603.08575
    """
    _n_transform_param = 4

    def __init__(self, img_size, crop_size, n_appearance,
                 transition, input_encoder, glimpse_encoder, glimpse_decoder, transform_estimator, steps_predictor,
                 discrete_steps=True, canvas_init=None, explore_eps=None, debug=False):
        """Creates the cell

        :param img_size: int tuple, size of the image
        :param crop_size: int tuple, size of the attention glimpse
        :param n_appearance: number of latent units describing the "what"
        :param transition: an RNN cell for maintaining the internal hidden state
        :param input_encoder: callable, encodes the original input image before passing it into the transition
        :param glimpse_encoder: callable, encodes the glimpse into latent representation
        :param glimpse_decoder: callable, decodes the glimpse from latent representation
        :param transform_estimator: callabe, transforms the hidden state into parameters for the spatial transformer
        :param steps_predictor: callable, predicts whether to take a step
        :param discrete_steps: boolean, steps are samples from a Bernoulli distribution if True; if False, all steps are
         taken and are weighted by the step probability
        :param canvas_init: float or None, initial value for the reconstructed image. If None, the canvas is black. If
         float, the canvas starts with a given value, which is trainable.
        :param explore_eps: float or None; if float, it has to be \in (0., .5); step probability is clipped between
         `explore_eps` and (1 - `explore_eps)
        :param debug: boolean, adds checks for NaNs in the inputs to distributions
        """

        super(AIRCell, self).__init__(self.__class__.__name__)
        self._img_size = img_size
        self._n_pix = np.prod(self._img_size)
        self._crop_size = crop_size
        self._n_appearance = n_appearance
        self._transition = transition
        self._n_hidden = self._transition.output_size[0]

        self._sample_presence = discrete_steps
        self._explore_eps = explore_eps
        self._debug = debug

        with self._enter_variable_scope():
            self._canvas = tf.zeros(self._img_size, dtype=tf.float32)
            if canvas_init is not None:
                self._canvas_value = tf.get_variable('canvas_value', dtype=tf.float32, initializer=canvas_init)
                self._canvas += self._canvas_value

            transform_constraints = snt.AffineWarpConstraints.no_shear_2d()

            self._spatial_transformer = SpatialTransformer(img_size, crop_size, transform_constraints)
            self._inverse_transformer = SpatialTransformer(img_size, crop_size, transform_constraints, inverse=True)

            self._transform_estimator = transform_estimator(self._n_transform_param)
            self._input_encoder = input_encoder()
            self._glimpse_encoder = glimpse_encoder()
            self._glimpse_decoder = glimpse_decoder(crop_size)

            self._what_distrib = ParametrisedGaussian(n_appearance, scale_offset=-1.,
                                                      validate_args=self._debug, allow_nan_stats=not self._debug)

            self._steps_predictor = steps_predictor()
            self._rnn_projection = Affine(self._n_hidden, transfer=None)

    @property
    def state_size(self):
        return [
            np.prod(self._img_size),  # image
            np.prod(self._img_size),  # canvas
            self._n_appearance,  # what
            self._n_transform_param,  # where
            self._transition.state_size,  # hidden state of the rnn
            1,  # presence
        ]

    @property
    def output_size(self):
        return [
            np.prod(self._img_size),  # canvas
            np.prod(self._crop_size),  # glimpse
            self._n_appearance,  # what code
            self._n_appearance,  # what loc
            self._n_appearance,  # what scale
            self._n_transform_param,  # where code
            self._n_transform_param,  # where loc
            self._n_transform_param,  # where scale
            1,  # presence prob
            1  # presence
        ]

    @property
    def output_names(self):
        return 'canvas glimpse what what_loc what_scale where where_loc where_scale presence_prob presence'.split()

    def initial_state(self, img):
        batch_size = img.get_shape().as_list()[0]
        hidden_state = self._transition.initial_state(batch_size, tf.float32, trainable=True)

        where_code = tf.get_variable('where_init', shape=[1, self._n_transform_param], dtype=tf.float32)

        what_code = tf.get_variable('what_init', shape=[1, self._n_appearance], dtype=tf.float32)

        flat_canvas = tf.reshape(self._canvas, (1, self._n_pix))

        where_code, what_code, flat_canvas = (tf.tile(i, (batch_size, 1)) for i in (where_code, what_code, flat_canvas))

        flat_img = tf.reshape(img, (batch_size, self._n_pix))
        init_presence = tf.ones((batch_size, 1), dtype=tf.float32)
        return [flat_img, flat_canvas,
                what_code, where_code, hidden_state, init_presence]

    def _build(self, inpt, state):
        """Input is unused; it's only to force a maximum number of steps"""

        img_flat, canvas_flat, what_code, where_code, hidden_state, presence = state
        img = tf.reshape(img_flat, (-1,) + tuple(self._img_size))

        inpt_encoding = img
        inpt_encoding = self._input_encoder(inpt_encoding)

        with tf.variable_scope('rnn_inpt'):
            rnn_inpt = tf.concat((inpt_encoding, what_code, where_code, presence), -1)
            rnn_inpt = self._rnn_projection(rnn_inpt)
            hidden_output, hidden_state = self._transition(rnn_inpt, hidden_state)

        where_param = self._transform_estimator(hidden_output)
        where_distrib = NormalWithSoftplusScale(*where_param,
                                                validate_args=self._debug, allow_nan_stats=not self._debug)
        where_loc, where_scale = where_distrib.loc, where_distrib.scale
        where_code = where_distrib.sample()

        cropped = self._spatial_transformer(img, where_code)

        with tf.variable_scope('presence'):
            presence_prob = self._steps_predictor(hidden_output)

            if self._explore_eps is not None:
                clipped_prob = tf.clip_by_value(presence_prob, self._explore_eps, 1. - self._explore_eps)
                presence_prob = tf.stop_gradient(clipped_prob - presence_prob) + presence_prob

            if self._sample_presence:
                presence_distrib = Bernoulli(probs=presence_prob, dtype=tf.float32,
                                             validate_args=self._debug, allow_nan_stats=not self._debug)

                new_presence = presence_distrib.sample()
                presence *= new_presence

            else:
                presence = presence_prob

        what_params = self._glimpse_encoder(cropped)
        what_distrib = self._what_distrib(what_params)
        what_loc, what_scale = what_distrib.loc, what_distrib.scale
        what_code = what_distrib.sample()
        decoded = self._glimpse_decoder(tf.concat([what_code, tf.stop_gradient(where_code)], -1))
        inversed = self._inverse_transformer(decoded, where_code)

        with tf.variable_scope('rnn_outputs'):
            inversed_flat = tf.reshape(inversed, (-1, self._n_pix))

            canvas_flat = canvas_flat + presence * inversed_flat  # * novelty_flat
            decoded_flat = tf.reshape(decoded, (-1, np.prod(self._crop_size)))

        output = [canvas_flat, decoded_flat, what_code, what_loc, what_scale, where_code, where_loc, where_scale,
                  presence_prob, presence]
        state = [img_flat, canvas_flat,
                 what_code, where_code, hidden_state, presence]
        return output, state