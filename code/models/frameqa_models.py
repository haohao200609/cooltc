import tensorflow as tf
from tensorflow.python.client import device_lib
from frameqa_base import *
from util import log
from tensorflow.python.ops import rnn_cell
from models.rnn_cell.rnn_cell import BasicLSTMCell_LayerNorm as cell_class
from ops import *

import time
import numpy as np

class FrameQAResnet(FrameQABase):
    @staticmethod
    def add_flags(FLAGS):
        FLAGS.image_feature_net = "resnet"
        FLAGS.layer = "pool5"
class FrameQAResnetEvaluator(FrameQABaseEvaluator):
    pass
class FrameQAResnetTrainer(FrameQABaseTrainer):
    pass




class FrameQAC3D(FrameQABase):
    @staticmethod
    def add_flags(FLAGS):
        FLAGS.image_feature_net = "c3d"
        FLAGS.layer = "fc6"
class FrameQAC3DEvaluator(FrameQABaseEvaluator):
    pass
class FrameQAC3DTrainer(FrameQABaseTrainer):
    pass

class FrameQAConcat(FrameQABase):
    @staticmethod
    def add_flags(FLAGS):
        FLAGS.image_feature_net = "concat"
        FLAGS.layer = "fc"
class FrameQAConcatEvaluator(FrameQABaseEvaluator):
    pass
class FrameQAConcatTrainer(FrameQABaseTrainer):
    pass

class FrameQATp(FrameQABase):
    @staticmethod
    def add_flags(FLAGS):
        FLAGS.image_feature_net = "concat"
        FLAGS.layer = "fc"

    def build_graph(self,
                    video,
                    video_mask,
                    question,
                    question_mask,
                    answer,
                    train_flag):


        self.video = video  # [batch_size, length, kernel, kernel, channel]
        self.video_mask = video_mask  # [batch_size, length]
        self.caption = question
        self.caption_mask = question_mask  # [batch_size, length]
        self.train_flag = train_flag  # boolean
        self.answer = answer

        # word embedding and dropout, etc.
        if self.word_embed is not None:
            self.word_embed_t = tf.constant(self.word_embed, dtype=tf.float32, name="word_embed")
        else:
            self.word_embed_t = tf.get_variable("Word_embed",
                                                [self.vocabulary_size, self.word_dim],
                                                initializer=tf.random_normal_initializer(stddev=0.1))
        self.dropout_keep_prob_cell_input_t = tf.constant(self.dropout_keep_prob_cell_input)
        self.dropout_keep_prob_cell_output_t = tf.constant(self.dropout_keep_prob_cell_output)
        self.dropout_keep_prob_fully_connected_t = tf.constant(self.dropout_keep_prob_fully_connected)
        self.dropout_keep_prob_output_t = tf.constant(self.dropout_keep_prob_output)
        self.dropout_keep_prob_image_embed_t = tf.constant(self.dropout_keep_prob_image_embed)

        with tf.variable_scope("conv_image_emb"):
            self.r_shape = tf.reshape(self.video, [-1, self.kernel_size, self.kernel_size, self.channel_size])
            #  [batch_size*length, kernel_size, kernel_size, channel_size]
            self.pooled_feat = tf.nn.avg_pool(self.r_shape,
                                              ksize=[1, self.kernel_size, self.kernel_size, 1],
                                              strides=[1, self.kernel_size, self.kernel_size, 1],
                                              padding="SAME")
            #  [batch_size*length, 1, 1, channel_size]
            self.squeezed_feat = tf.squeeze(self.pooled_feat)
            #  [batch_size*length, channel_size]
            self.embedded_feat = tf.reshape(self.squeezed_feat, [self.batch_size,
                                                                 self.lstm_steps,
                                                                 self.channel_size])
            #  [batch_size, length, channel_size]
            self.embedded_feat_drop = tf.nn.dropout(self.embedded_feat, self.dropout_keep_prob_image_embed_t)

        with tf.variable_scope("video_rnn") as scope:
            self.video_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            self.vid_initial_state = tf.zeros([self.batch_size, self.video_cell.state_size])
            self.vid_rnn_states = [self.vid_initial_state]

            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()
                new_output, new_state = self.video_cell(self.embedded_feat_drop[:, i, :],
                                                        self.vid_rnn_states[-1])
                self.vid_rnn_states.append(new_state * tf.expand_dims(self.video_mask[:, i], 1))

            self.vid_states = [
                tf.concat(1, [tf.slice(vid_rnn_state, [0,0], [-1,self.hidden_dim]),
                              tf.slice(vid_rnn_state, [0,2*self.hidden_dim], [-1,self.hidden_dim])])
                for vid_rnn_state in self.vid_rnn_states[1:]]

        with tf.variable_scope("word_emb"):
            with tf.device("/cpu:0"):
                self.embedded_captions = tf.nn.embedding_lookup(self.word_embed_t, self.caption)
                # [batch_size, length, word_dim]
                self.embedded_start_word = tf.nn.embedding_lookup(self.word_embed_t,
                                                                  tf.ones([self.batch_size], dtype=tf.int32))
        with tf.variable_scope("caption_rnn") as scope:
            self.caption_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            self.cap_initial_state = self.vid_rnn_states[-1]
            self.cap_rnn_states = [self.cap_initial_state]

            current_embedded_y = self.embedded_start_word
            self.alphas = []
            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()

                new_output, new_state = self.caption_cell(current_embedded_y,
                                                          self.cap_rnn_states[-1])
                self.cap_rnn_states.append(new_state)
                current_embedded_y = self.embedded_captions[:, i, :]

        with tf.variable_scope("merge") as scope:
            rnn_final_state = tf.concat(1, [
                tf.slice(self.cap_rnn_states[-1], [0,0], [-1,self.hidden_dim]),
                tf.slice(self.cap_rnn_states[-1], [0,2*self.hidden_dim], [-1,self.hidden_dim])])
            vid_att, alpha = self.attention(rnn_final_state, self.vid_states)
            final_embed = tf.add(tf.nn.tanh(linear(vid_att, 2*self.hidden_dim)),
                                 rnn_final_state)

        with tf.variable_scope("loss") as scope:
            rnnW = tf.get_variable(
                "W",
                [2*self.hidden_dim, self.answer_size],
                initializer=tf.random_normal_initializer(stddev=0.1))
            rnnb = tf.get_variable(
                "b",
                [self.answer_size],
                initializer=tf.constant_initializer(0.0))
            embed_state = tf.nn.xw_plus_b(final_embed, rnnW,rnnb)

            labels = self.answer
            indices = tf.expand_dims(tf.range(0, self.batch_size, 1), 1)
            labels_with_index = tf.concat(1, [indices, labels])

            onehot_labels = tf.sparse_to_dense(labels_with_index,
                                                tf.pack([self.batch_size, self.answer_size]),
                                                sparse_values=1.0,
                                                default_value=0)
            cross_entropy_loss = tf.nn.softmax_cross_entropy_with_logits(embed_state, onehot_labels)

            self.mean_loss = tf.reduce_mean(cross_entropy_loss, name="t_loss")

        with tf.variable_scope("accuracy"):
            # prediction tensor on test phase
            self.predictions = tf.argmax(
                tf.reshape(embed_state, [self.batch_size, self.answer_size]),
                dimension=1, name='argmax_predictions'
            )
            self.predictions.get_shape().assert_is_compatible_with([self.batch_size])

            self.correct_predictions = tf.cast(tf.equal(
                tf.reshape(self.predictions, [self.batch_size, 1]),
                tf.cast(self.answer,tf.int64)), tf.int32)
            self.acc = tf.reduce_mean(tf.cast(self.correct_predictions, "float"), name="accuracy")

    def attention(self, prev_hidden, vid_states):
        packed = tf.pack(vid_states)
        packed = tf.transpose(packed, [1,0,2])
        vid_2d = tf.reshape(packed, [-1, self.hidden_dim*2])
        sent_2d = tf.tile(prev_hidden, [1, self.lstm_steps])
        sent_2d = tf.reshape(sent_2d, [-1, self.hidden_dim*2])
        preact = tf.add(linear(sent_2d, self.hidden_dim, name="preatt_sent"),
                        linear(vid_2d, self.hidden_dim, name="preadd_vid"))
        score = linear(tf.nn.tanh(preact), 1, name="preatt")
        score_2d = tf.reshape(score, [-1, self.lstm_steps])
        alpha = tf.nn.softmax(score_2d)
        alpha_3d = tf.reshape(alpha, [-1, self.lstm_steps, 1])
        return tf.reduce_sum(packed * alpha_3d, 1), alpha

class FrameQATpEvaluator(FrameQABaseEvaluator):
    pass
class FrameQATpTrainer(FrameQABaseTrainer):
    pass

class FrameQASp(FrameQABase):
    @staticmethod
    def add_flags(FLAGS):
        FLAGS.image_feature_net = "concat"
        FLAGS.layer = "conv"

    def build_graph(self,
                    video,
                    video_mask,
                    caption,
                    caption_mask,
                    answer,
                    train_flag):

        self.video = video  # [batch_size, length, kernel, kernel, channel]
        self.video_mask = video_mask  # [batch_size, length]
        self.caption = caption  # [batch_size, 5, length]
        self.caption_mask = caption_mask  # [batch_size, 5, length]
        self.train_flag = train_flag  # boolean
        self.answer = answer

        # word embedding and dropout, etc.
        if self.word_embed is not None:
            self.word_embed_t = tf.constant(self.word_embed, dtype=tf.float32, name="word_embed")
        else:
            self.word_embed_t = tf.get_variable("Word_embed",
                                                [self.vocabulary_size, self.word_dim],
                                                initializer=tf.random_normal_initializer(stddev=0.1))
        self.dropout_keep_prob_cell_input_t = tf.constant(self.dropout_keep_prob_cell_input)
        self.dropout_keep_prob_cell_output_t = tf.constant(self.dropout_keep_prob_cell_output)
        self.dropout_keep_prob_fully_connected_t = tf.constant(self.dropout_keep_prob_fully_connected)
        self.dropout_keep_prob_output_t = tf.constant(self.dropout_keep_prob_output)
        self.dropout_keep_prob_image_embed_t = tf.constant(self.dropout_keep_prob_image_embed)


        for idx, device in enumerate(self.devices):
            with tf.device("/%s" % device):
                if idx > 0:
                    tf.get_variable_scope().reuse_variables()
                from_idx = self.batch_size_per_gpu*idx

                video = tf.slice(self.video, [from_idx,0,0,0,0],
                                    [self.batch_size_per_gpu,-1,-1,-1,-1])
                video_mask = tf.slice(self.video_mask, [from_idx,0],
                                        [self.batch_size_per_gpu,-1])
                caption = tf.slice(self.caption, [from_idx,0],
                                        [self.batch_size_per_gpu,-1])
                caption_mask = tf.slice(self.caption_mask, [from_idx,0],
                                            [self.batch_size_per_gpu,-1])
                answer = tf.slice(self.answer, [from_idx,0],
                                            [self.batch_size_per_gpu,-1])

                self.build_graph_single_gpu(video, video_mask, caption,
                                            caption_mask, answer, idx)

        self.mean_loss = tf.reduce_mean(tf.pack(self.mean_loss_list, axis=0))
        self.alpha = tf.pack(self.alpha_list, axis=0)
        self.predictions = tf.pack(self.predictions_list, axis=0)
        self.correct_predictions = tf.pack(self.correct_predictions_list, axis=0)
        self.acc = tf.reduce_mean(tf.pack(self.acc_list, axis=0))

    def build_graph_single_gpu(self, video, video_mask, caption, caption_mask, answer, idx):

        with tf.variable_scope("word_emb"):
            with tf.device("/cpu:0"):
                embedded_captions = tf.nn.embedding_lookup(self.word_embed_t, caption)
                # [batch_size, length, word_dim]
                embedded_start_word = tf.nn.embedding_lookup(
                    self.word_embed_t, tf.ones([self.batch_size_per_gpu], dtype=tf.int32))

        with tf.variable_scope("caption_rnn") as scope:
            caption_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            cap_initial_state = tf.zeros([self.batch_size_per_gpu, caption_cell.state_size])
            cap_rnn_states = [cap_initial_state]
            current_embedded_y = embedded_start_word
            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()

                new_output, new_state = caption_cell(current_embedded_y, cap_rnn_states[-1])
                cap_rnn_states.append(new_state)
                current_embedded_y = embedded_captions[:, i, :]

        with tf.variable_scope("merge_emb") as scope:
            rnn_final_state1 = tf.concat(1, [
                tf.slice(cap_rnn_states[-1], [0,0], [-1,self.hidden_dim]),
                tf.slice(cap_rnn_states[-1], [0,2*self.hidden_dim], [-1,self.hidden_dim])])

            fc_sent = linear(rnn_final_state1, 512, name="fc_sent")
            fc_sent_tiled = tf.tile(fc_sent, [1, self.lstm_steps*7*7])
            fc_sent_tiled = tf.reshape(fc_sent_tiled, [-1, 512])

            video_2d = tf.reshape(video, [self.batch_size_per_gpu*self.lstm_steps*7*7,
                                            self.channel_size])
            fc_vid = linear(video_2d, 512, name="fc_vid")
            pooled = tf.tanh(tf.add(fc_vid, fc_sent_tiled))

        with tf.variable_scope("att_image_emb"):
            pre_alpha = linear(pooled, 1, name="pre_alpha")
            pre_alpha = tf.reshape(pre_alpha, [-1, 7*7])
            alpha = tf.nn.softmax(pre_alpha)
            alpha = tf.reshape(alpha, [self.batch_size_per_gpu*self.lstm_steps, 7*7, 1])
            self.alpha_list.append(
                tf.reshape(alpha, [self.batch_size_per_gpu, self.lstm_steps, 7*7]))

            batch_pre_att = tf.reshape(video, [self.batch_size_per_gpu*self.lstm_steps,
                                                7*7, self.channel_size])
            embedded_feat = tf.reduce_sum(batch_pre_att * alpha, 1)
            embedded_feat = tf.reshape(embedded_feat, [self.batch_size_per_gpu, self.lstm_steps, self.channel_size])

            #  [batch_size, length, channel_size]
            embedded_feat_drop = tf.nn.dropout(
                embedded_feat, self.dropout_keep_prob_image_embed_t)

        with tf.variable_scope("video_rnn") as scope:
            video_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            vid_initial_state = tf.zeros([self.batch_size_per_gpu, video_cell.state_size])
            vid_rnn_states = [vid_initial_state]
            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()
                new_output, new_state = video_cell(embedded_feat_drop[:, i, :],
                                                   vid_rnn_states[-1])
                vid_rnn_states.append(new_state * tf.expand_dims(video_mask[:, i], 1))

            vid_states = [
                tf.concat(1, [tf.slice(vid_rnn_state, [0,0], [-1,self.hidden_dim]),
                              tf.slice(vid_rnn_state, [0,2*self.hidden_dim], [-1,self.hidden_dim])])
                for vid_rnn_state in vid_rnn_states[1:]]

        with tf.variable_scope("caption_rnn") as scope:
            scope.reuse_variables()
            cell_class = self.cell_class_map.get(self.cell_class)
            one_cell = rnn_cell.DropoutWrapper(
                cell_class(self.hidden_dim),
                input_keep_prob=self.dropout_keep_prob_cell_input_t,
                output_keep_prob=self.dropout_keep_prob_cell_output_t)
            caption_cell = rnn_cell.MultiRNNCell([one_cell] * self.num_layers)
            # Build the recurrence.
            cap_initial_state = vid_rnn_states[-1]
            cap_rnn_states = [cap_initial_state]

            current_embedded_y = embedded_start_word
            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()

                new_output, new_state = caption_cell(current_embedded_y, cap_rnn_states[-1])

                cap_rnn_states.append(new_state)
                current_embedded_y = embedded_captions[:, i, :]

        with tf.variable_scope("loss") as scope:
            rnn_final_state = tf.concat(1, [
                tf.slice(cap_rnn_states[-1], [0,0], [-1,self.hidden_dim]),
                tf.slice(cap_rnn_states[-1], [0,2*self.hidden_dim], [-1,self.hidden_dim])])
            rnnW = tf.get_variable(
                "W",
                [2*self.hidden_dim, self.answer_size],
                initializer=tf.random_normal_initializer(stddev=0.1))
            rnnb = tf.get_variable(
                "b",
                [self.answer_size],
                initializer=tf.constant_initializer(0.0))
            embed_state = tf.nn.xw_plus_b(rnn_final_state,rnnW,rnnb)

            labels = answer
            indices = tf.expand_dims(tf.range(0, self.batch_size_per_gpu, 1), 1)
            labels_with_index = tf.concat(1, [indices, labels])

            onehot_labels = tf.sparse_to_dense(labels_with_index,
                                                tf.pack([self.batch_size_per_gpu, self.answer_size]),
                                                sparse_values=1.0,
                                                default_value=0)
            cross_entropy_loss = tf.nn.softmax_cross_entropy_with_logits(embed_state, onehot_labels)

            mean_loss = tf.reduce_mean(cross_entropy_loss, name="t_loss")
            self.mean_loss_list.append(mean_loss)

        with tf.variable_scope("accuracy"):
            # prediction tensor on test phase
            predictions = tf.argmax(
                tf.reshape(embed_state, [self.batch_size_per_gpu, self.answer_size]),
                dimension=1, name='argmax_predictions'
            )
            predictions.get_shape().assert_is_compatible_with([self.batch_size_per_gpu])

            # TODO not implemented here. do we need to exploit self.answer?
            correct_predictions = tf.cast(tf.equal(
                tf.reshape(predictions, [self.batch_size_per_gpu, 1]),
                tf.cast(answer,tf.int64)), tf.int32)
            acc = tf.reduce_mean(tf.cast(correct_predictions, "float"), name="accuracy_%d"%idx)

            self.predictions_list.append(predictions)
            self.correct_predictions_list.append(correct_predictions)
            self.acc_list.append(acc)

class FrameQASpEvaluator(FrameQABaseEvaluator):
    pass
class FrameQASpTrainer(FrameQABaseTrainer):
    pass

class FrameQASpTp(FrameQABase):
    @staticmethod
    def add_flags(FLAGS):
        FLAGS.image_feature_net = "concat"
        FLAGS.layer = "conv"

    def build_graph(self,
                    video,
                    video_mask,
                    caption,
                    caption_mask,
                    answer,
                    train_flag):

        self.video = video  # [batch_size, length, kernel, kernel, channel]
        self.video_mask = video_mask  # [batch_size, length]
        self.caption = caption  # [batch_size, 5, length]
        self.caption_mask = caption_mask  # [batch_size, 5, length]
        self.answer = answer
        self.train_flag = train_flag  # boolean

        # word embedding and dropout, etc.
        if self.word_embed is not None:
            self.word_embed_t = tf.constant(self.word_embed, dtype=tf.float32, name="word_embed")
        else:
            self.word_embed_t = tf.get_variable("Word_embed",
                                                [self.vocabulary_size, self.word_dim],
                                                initializer=tf.random_normal_initializer(stddev=0.1))
        self.dropout_keep_prob_cell_input_t = tf.constant(self.dropout_keep_prob_cell_input)
        self.dropout_keep_prob_cell_output_t = tf.constant(self.dropout_keep_prob_cell_output)
        self.dropout_keep_prob_fully_connected_t = tf.constant(self.dropout_keep_prob_fully_connected)
        self.dropout_keep_prob_output_t = tf.constant(self.dropout_keep_prob_output)
        self.dropout_keep_prob_image_embed_t = tf.constant(self.dropout_keep_prob_image_embed)

        for idx, device in enumerate(self.devices):
            with tf.device("/%s" % device):
                if idx > 0:
                    tf.get_variable_scope().reuse_variables()

                from_idx = self.batch_size_per_gpu*idx

                video = tf.slice(self.video, [from_idx,0,0,0,0],
                                    [self.batch_size_per_gpu,-1,-1,-1,-1])
                video_mask = tf.slice(self.video_mask, [from_idx,0],
                                        [self.batch_size_per_gpu,-1])
                caption = tf.slice(self.caption, [from_idx,0],
                                        [self.batch_size_per_gpu,-1])
                caption_mask = tf.slice(self.caption_mask, [from_idx,0],
                                            [self.batch_size_per_gpu,-1])
                answer = tf.slice(self.answer, [from_idx,0],
                                            [self.batch_size_per_gpu,-1])

                self.build_graph_single_gpu(video, video_mask, caption,
                                            caption_mask, answer, idx)

        self.mean_loss = tf.reduce_mean(tf.pack(self.mean_loss_list, axis=0))
        self.alpha = tf.pack(self.alpha_list, axis=0)
        self.predictions = tf.pack(self.predictions_list, axis=0)
        self.correct_predictions = tf.pack(self.correct_predictions_list, axis=0)
        self.acc = tf.reduce_mean(tf.pack(self.acc_list, axis=0))


    def build_graph_single_gpu(self, video, video_mask, caption, caption_mask, answer, idx):

        with tf.variable_scope("word_emb"):
            with tf.device("/cpu:0"):
                embedded_captions = tf.nn.embedding_lookup(self.word_embed_t, caption)
                # [batch_size, length, word_dim]
                embedded_start_word = tf.nn.embedding_lookup(
                    self.word_embed_t, tf.ones([self.batch_size_per_gpu], dtype=tf.int32))


        with tf.variable_scope("caption_rnn") as scope:
            caption_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            cap_initial_state = tf.zeros([self.batch_size_per_gpu, caption_cell.state_size])
            cap_rnn_states = [cap_initial_state]
            current_embedded_y = embedded_start_word
            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()

                new_output, new_state = caption_cell(current_embedded_y, cap_rnn_states[-1])
                cap_rnn_states.append(new_state)
                current_embedded_y = embedded_captions[:, i, :]

        def spatio_att():
            with tf.variable_scope("merge_emb") as scope:
                rnn_final_state1 = tf.concat(1, [
                    tf.slice(cap_rnn_states[-1], [0,0], [-1,self.hidden_dim]),
                    tf.slice(cap_rnn_states[-1], [0,2*self.hidden_dim], [-1,self.hidden_dim])])

                fc_sent = linear(rnn_final_state1, 512, name="fc_sent")
                fc_sent_tiled = tf.tile(fc_sent, [1, self.lstm_steps*7*7])
                fc_sent_tiled = tf.reshape(fc_sent_tiled, [-1, 512])

                video_2d = tf.reshape(video, [self.batch_size_per_gpu*self.lstm_steps*7*7,
                                                self.channel_size])
                fc_vid = linear(video_2d, 512, name="fc_vid")
                pooled = tf.tanh(tf.add(fc_vid, fc_sent_tiled))

                pre_alpha = linear(pooled, 1, name="pre_alpha")
                pre_alpha = tf.reshape(pre_alpha, [-1, 7*7])
                alpha = tf.nn.softmax(pre_alpha)
                alpha = tf.reshape(alpha, [self.batch_size_per_gpu*self.lstm_steps, 7*7, 1])
                return alpha
        def const_att():
            return tf.constant(1./7*7, dtype=tf.float32, shape=[self.batch_size_per_gpu*self.lstm_steps, 7*7, 1])

        alpha = tf.cond(self.train_step < self.N_PRETRAIN, const_att, spatio_att)
        self.alpha_list.append(tf.reshape(alpha, [self.batch_size_per_gpu, self.lstm_steps, 7*7]))

        with tf.variable_scope("att_image_emb"):
            batch_pre_att = tf.reshape(video, [self.batch_size_per_gpu*self.lstm_steps,
                                                7*7, self.channel_size])
            embedded_feat = tf.reduce_sum(batch_pre_att * alpha, 1)
            embedded_feat = tf.reshape(embedded_feat, [self.batch_size_per_gpu, self.lstm_steps, self.channel_size])

            #  [batch_size, length, channel_size]
            embedded_feat_drop = tf.nn.dropout(
                embedded_feat, self.dropout_keep_prob_image_embed_t)

        with tf.variable_scope("video_rnn") as scope:
            video_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            vid_initial_state = tf.zeros([self.batch_size_per_gpu, video_cell.state_size])
            vid_rnn_states = [vid_initial_state]

            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()
                new_output, new_state = video_cell(embedded_feat_drop[:, i, :],
                                                   vid_rnn_states[-1])
                vid_rnn_states.append(new_state * tf.expand_dims(video_mask[:, i], 1))

            vid_states = [
                tf.concat(1, [tf.slice(vid_rnn_state, [0,0], [-1,self.hidden_dim]),
                              tf.slice(vid_rnn_state, [0,2*self.hidden_dim], [-1,self.hidden_dim])])
                for vid_rnn_state in vid_rnn_states[1:]]

        with tf.variable_scope("caption_rnn") as scope:
            scope.reuse_variables()
            caption_cell = rnn_cell.MultiRNNCell([self.get_rnn_cell()] * self.num_layers)
            # Build the recurrence.
            cap_initial_state = vid_rnn_states[-1]
            cap_rnn_states = [cap_initial_state]

            current_embedded_y = embedded_start_word

            for i in range(self.lstm_steps):
                if i > 0:
                    scope.reuse_variables()

                new_output, new_state = caption_cell(current_embedded_y, cap_rnn_states[-1])
                cap_rnn_states.append(new_state)
                current_embedded_y = embedded_captions[:, i, :]

        with tf.variable_scope("merge") as scope:
            rnn_final_state = tf.concat(1, [
                tf.slice(cap_rnn_states[-1], [0,0], [-1,self.hidden_dim]),
                tf.slice(cap_rnn_states[-1], [0,2*self.hidden_dim], [-1,self.hidden_dim])])
            vid_att, alpha = self.attention(rnn_final_state, vid_states)
            final_embed = tf.add(tf.nn.tanh(linear(vid_att, 2*self.hidden_dim)),
                                 rnn_final_state)

        with tf.variable_scope("loss") as scope:
            rnnW = tf.get_variable(
                "W",
                [2*self.hidden_dim, self.answer_size],
                initializer=tf.random_normal_initializer(stddev=0.1))
            rnnb = tf.get_variable(
                "b",
                [self.answer_size],
                initializer=tf.constant_initializer(0.0))
            embed_state = tf.nn.xw_plus_b(final_embed,rnnW,rnnb)

            labels = answer
            indices = tf.expand_dims(tf.range(0, self.batch_size_per_gpu, 1), 1)
            labels_with_index = tf.concat(1, [indices, labels])

            onehot_labels = tf.sparse_to_dense(labels_with_index,
                                                tf.pack([self.batch_size_per_gpu, self.answer_size]),
                                                sparse_values=1.0,
                                                default_value=0)
            cross_entropy_loss = tf.nn.softmax_cross_entropy_with_logits(embed_state, onehot_labels)

            mean_loss = tf.reduce_mean(cross_entropy_loss, name="t_loss")
            self.mean_loss_list.append(mean_loss)


        with tf.variable_scope("accuracy"):
            # prediction tensor on test phase
            predictions = tf.argmax(
                tf.reshape(embed_state, [self.batch_size_per_gpu, self.answer_size]),
                dimension=1, name='argmax_predictions'
            )
            predictions.get_shape().assert_is_compatible_with([self.batch_size_per_gpu])

            correct_predictions = tf.cast(tf.equal(
                tf.reshape(predictions, [self.batch_size_per_gpu, 1]),
                tf.cast(answer,tf.int64)), tf.int32)
            acc = tf.reduce_mean(tf.cast(correct_predictions, "float"), name="accuracy_%d"%idx)

            self.predictions_list.append(predictions)
            self.correct_predictions_list.append(correct_predictions)
            self.acc_list.append(acc)

    def attention(self, prev_hidden, vid_states):
        packed = tf.pack(vid_states)
        packed = tf.transpose(packed, [1,0,2])
        vid_2d = tf.reshape(packed, [-1, self.hidden_dim*2])
        sent_2d = tf.tile(prev_hidden, [1, self.lstm_steps])
        sent_2d = tf.reshape(sent_2d, [-1, self.hidden_dim*2])
        preact = tf.add(linear(sent_2d, self.hidden_dim, name="preatt_sent"),
                        linear(vid_2d, self.hidden_dim, name="preadd_vid"))
        score = linear(tf.nn.tanh(preact), 1, name="preatt")
        score_2d = tf.reshape(score, [-1, self.lstm_steps])
        alpha = tf.nn.softmax(score_2d)
        alpha_3d = tf.reshape(alpha, [-1, self.lstm_steps, 1])
        return tf.reduce_sum(packed * alpha_3d, 1), alpha

class FrameQASpTpEvaluator(FrameQABaseEvaluator):
    pass
class FrameQASpTpTrainer(FrameQABaseTrainer):
    pass
