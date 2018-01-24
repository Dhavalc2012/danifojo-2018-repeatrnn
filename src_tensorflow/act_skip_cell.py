from __future__ import division
from __future__ import print_function

import tensorflow as tf
from tensorflow.contrib.rnn import RNNCell
from tensorflow.contrib.rnn import static_rnn
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.framework import ops


def _binary_round(x, epsilon=0):
    """
    Rounds a tensor whose values are in [0,1] to a tensor with values in {0, 1},
    using the straight through estimator for the gradient.

    Based on http://r2rt.com/binary-stochastic-neurons-in-tensorflow.html
    :param x: input tensor
    :return: y=round(x-0.5+epsilon) with gradients defined by the identity mapping (y=x)
    """
    g = tf.get_default_graph()
    with ops.name_scope("BinaryRound") as name:
        with g.gradient_override_map({"Round": "Identity"}):
            return tf.round(x-0.5+epsilon, name=name)
            # condition = tf.greater_equal(x, 1-epsilon)
            # return tf.where(condition, tf.ones_like(x), tf.zeros_like(x), name=name)


class ACTCell(RNNCell):
    def __init__(self, num_units, cell, batch_size, epsilon, mu=5,
                 max_computation=100, initial_bias=-1., state_is_tuple=False):

        self.batch_size = batch_size
        self.ones = tf.fill([self.batch_size], tf.constant(1.0, dtype=tf.float32))
        self.cell = cell
        self.mu = mu
        self._num_units = num_units
        self.max_computation = max_computation
        self.ACT_steps = []
        self.initial_bias = initial_bias
        self.last_p = tf.zeros([batch_size])
        self.last_s = tf.zeros([batch_size, num_units])

        if hasattr(self.cell, "_state_is_tuple"):
            self._state_is_tuple = self.cell._state_is_tuple
        else:
            self._state_is_tuple = state_is_tuple

        if self._state_is_tuple:
            self.last_output = tf.zeros([batch_size, num_units//2])
        else:
            self.last_output = tf.zeros([batch_size, num_units])


    @property
    def input_size(self):
        return self._num_units

    @property
    def output_size(self):
        if self._state_is_tuple:
            return self._num_units//2
        else:
            return self._num_units

    @property
    def state_size(self):
        return self._num_units

    def __call__(self, inputs, state, timestep=0, scope=None):
        if self._state_is_tuple:
            state = tf.concat(state, 1)

        with vs.variable_scope(scope or type(self).__name__):
            prob = tf.fill([self.batch_size], tf.constant(0.0, dtype=tf.float32), "prob")
            prob_compare = tf.zeros_like(prob, tf.float32, name="prob_compare")
            counter = tf.zeros_like(prob, tf.float32, name="counter")
            acc_outputs = tf.fill([self.batch_size, self.output_size], 0.0, name='output_accumulator')
            acc_outputs += _binary_round(self.last_p)*self.last_output
            acc_states = tf.zeros_like(state, tf.float32, name="state_accumulator")
            acc_states += _binary_round(self.last_p)*self.last_s
            acc_steps = tf.fill([self.batch_size], tf.constant(0.0, dtype=tf.float32), "steps")
            batch_mask = tf.fill([self.batch_size], True, name="batch_mask")

            def halting_predicate(batch_mask, prob_compare, prob,
                          counter, state, input, acc_output, acc_state, acc_steps, last_p):
                return tf.reduce_any(tf.less(prob_compare, self.ones))


            _, _, prob, iterations, _, _, output, next_state, total_steps, _ = \
                tf.while_loop(halting_predicate, self.act_step,
                              loop_vars=[batch_mask, prob_compare, prob,
                                         counter, state, inputs, acc_outputs, acc_states, acc_steps, self.last_p])

        # accumulate steps
        self.ACT_steps.append(tf.reduce_mean(total_steps))

        self.last_s = next_state
        if self._state_is_tuple:
            next_c, next_h = tf.split(next_state, 2, 1)
            next_state = tf.contrib.rnn.LSTMStateTuple(next_c, next_h)
        self.last_output = output
        self.last_p = prob-self.ones

        return output, next_state

    def calculate_ponder_cost(self):
        return tf.reduce_sum(tf.to_float(tf.add_n(self.ACT_steps)/len(self.ACT_steps)))

    def act_step(self, batch_mask, prob_compare, prob, counter, state, input, acc_outputs, acc_states, acc_steps, last_p):

        binary_flag = tf.cond(tf.reduce_all(tf.equal(prob, 0.0)),
                              lambda: tf.ones([self.batch_size, 1], dtype=tf.float32),
                              lambda: tf.zeros([self.batch_size, 1], dtype=tf.float32))

        input_with_flags = tf.concat([binary_flag, input], 1)
        if self._state_is_tuple:
            (c, h) = tf.split(state, 2, 1)
            state = tf.contrib.rnn.LSTMStateTuple(c, h)

        output, new_state = self.cell(input_with_flags, state)

        if self._state_is_tuple:
            with tf.variable_scope('sigmoid_activation_for_pondering'):
                p = tf.squeeze(tf.layers.dense(new_state[0], 1, activation=tf.sigmoid,
                                               use_bias=True,
                                               bias_initializer=tf.constant_initializer(self.initial_bias)),
                               squeeze_dims=1)
            new_state = tf.concat(new_state, 1)

        else:
            with tf.variable_scope('sigmoid_activation_for_pondering'):
                p = self.mu*tf.squeeze(tf.layers.dense(new_state, 1, activation=tf.sigmoid,
                                                       use_bias=True,
                                                       bias_initializer=tf.constant_initializer(self.initial_bias)),
                                       squeeze_dims=1)

        new_batch_mask = tf.less(prob + p, self.ones)
        new_float_mask = tf.cast(new_batch_mask, tf.float32)
        float_mask = tf.cast(batch_mask, tf.float32)

        prob = prob + p * float_mask
        prob_compare += p * float_mask

        counter += new_float_mask

        update_weight = tf.expand_dims(prob, -1)
        float_mask_exp = tf.expand_dims(float_mask, -1)

        acc_state = _binary_round(1-last_p, 0)*(new_state * _binary_round(update_weight, 0) * float_mask_exp) + acc_states
        acc_output = _binary_round(1-last_p, 0)*(output * _binary_round(update_weight, 0) * float_mask_exp) + acc_outputs
        acc_steps = ((1-_binary_round(prob, 0)) * float_mask) + acc_steps

        return [new_batch_mask, prob_compare, prob, counter, new_state, input, acc_output, acc_state, acc_steps, last_p]