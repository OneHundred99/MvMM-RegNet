# -*- coding: utf-8 -*-
"""
Unified Multi-Atlas Segmentation implementations using dense displacement fields for model construction and training.
The optimization is based on the multi-variate mixture model of the target image and atlas probabilistic labels.

@author: Xinzhe Luo
"""

from __future__ import print_function, division, absolute_import, unicode_literals

import math
import os
import random
import shutil
from torch.utils.data import DataLoader

from core.losses_2d import *
from core.metrics_2d import *
from core.networks_2d import *

tfd = tf.distributions
config = tf.ConfigProto(allow_soft_placement=True)
config.gpu_options.allow_growth = True
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')


class UnifiedMultiAtlasSegNet(object):
    """
    A unified multi-atlas segmentation network implementation.

    ToDo:
        transform the atlas label/prob tensors into shape [n_batch, *vol_shape, n_atlas, n_class]
    """

    def __init__(self, input_size: tuple = (64, 64), channels: int = 1, n_class: int = 2, n_atlas: int = 5,
                 n_subtypes: tuple = (2, 1,), cost_kwargs=None, aug_kwargs=None, **net_kwargs, ):
        """
        :param input_size: The input size for the network.
        :param channels: (Optional) number of channels in the input target image.
        :param n_class: (Optional) number of output labels.
        :param n_atlas: The number of atlases within the multivariate mixture model.
        :param n_subtypes: A tuple indicating the number of subtypes within each tissue class, with the first element
            corresponding to the background subtypes.
        :param cost_kwargs: (Optional) kwargs passed to the cost function, e.g. regularizer_type/auxiliary_cost_name.
        :param aug_kwargs: optional data augmentation arguments
        :param net_kwargs: optional network configuration arguments
        """
        # assert n_class == len(n_subtypes), "The length of the subtypes tuple must equal to the number of classes."
        if cost_kwargs is None:
            cost_kwargs = {}
        if aug_kwargs is None:
            aug_kwargs = {}

        # tf.reset_default_graph()
        self.input_size = input_size
        self.channels = channels
        self.n_class = n_class
        self.n_atlas = n_atlas
        self.n_subtypes = n_subtypes
        self.cost_kwargs = cost_kwargs
        self.aug_kwargs = aug_kwargs
        self.cost_name = cost_kwargs.get('cost_name', None)
        self.net_kwargs = net_kwargs
        self.prob_sigma = cost_kwargs.get('prob_sigma', (1, 2, 4, 8)) \
            if 'multi_scale' in self.cost_name else cost_kwargs.get('prob_sigma', (1,))
        self.prob_eps = cost_kwargs.get('prob_eps', math.exp(-3 ** 2 / 2))
        self.logger = net_kwargs.get("logger", logging)
        self.summaries = net_kwargs.get("summaries", True)
        # initialize regularizer
        self.regularizer_type = self.cost_kwargs.get("regularizer", None)
        self.net_regularizer = self.regularizer_type[0]
        self.regularization_coefficient = self.cost_kwargs.get("regularization_coefficient")

        # define placeholders for inputs
        with tf.name_scope('inputs'):
            # flag for training or inference
            self.train_phase = tf.placeholder(tf.bool, name='train_phase')
            # dropout rate
            self.dropout_rate = tf.placeholder(tf.float32, name='dropout_rate')
            # substructure prior weight
            prior_prob = cost_kwargs.pop("prior_prob", None)
            if prior_prob is None:
                prior_prob =  tf.cast(tf.fill([1, 1, 1, n_class], 1 / self.n_class), dtype=tf.float32)
            self.pi = tf.reshape(prior_prob, shape=[1, 1, 1, n_class], name='prior_prob')
            # input data
            self.data = {'target_image': tf.placeholder(tf.float32, [None, input_size[0], input_size[1], channels],
                                                        name='target_image'),
                         'target_label': tf.placeholder(tf.float32, [None, input_size[0], input_size[1], n_class],
                                                        name='target_label'),
                         'target_weight': tf.placeholder(tf.float32, [None, input_size[0], input_size[1], n_class],
                                                         name='target_weight'),
                         'atlases_image': tf.placeholder(tf.float32, [None, input_size[0], input_size[1], n_atlas,
                                                          channels], name='atlases_image'),
                         'atlases_label': tf.placeholder(tf.float32, [None, input_size[0], input_size[1], n_atlas, n_class],
                                                         name='atlases_label'),
                         'atlases_weight': tf.placeholder(tf.float32, [None, input_size[0], input_size[1], n_atlas, n_class],
                                                          name='atlases_weight')
                         }
            # random affine data augmentation
            self.augmented_data = self._get_augmented_data()

        # with tf.name_scope('gmm_inputs'):
        #     self.tau = [tf.placeholder(tf.float32, [None, n_subtypes[i]], name='tau_subtype%s' % i)
        #                 for i in range(n_class)]
        #     self.mu = [tf.placeholder(tf.float32, [None, n_subtypes[i]], name='mu_subtype%s' % i)
        #                for i in range(n_class)]
        #     self.sigma = [tf.placeholder(tf.float32, [None, n_subtypes[i]], name='sigma_subtype%s' % i)
        #                   for i in range(n_class)]

        with tf.variable_scope('network'):
            # compute the dense displacement fields of shape [n_batch, *vol_shape, n_atlas, 3]
            if self.net_kwargs['method'] == 'ddf_label':
                self.ddf, self.regularization_loss = create_ddf_label_net(self.augmented_data['target_image'],
                                                                           self.augmented_data['atlases_image'],
                                                                           dropout_rate=self.dropout_rate,
                                                                           n_atlas=self.n_atlas,
                                                                           train_phase=self.train_phase,
                                                                           regularizer=self.net_regularizer,
                                                                           **self.net_kwargs)

            elif self.net_kwargs['method'] == 'ddf_score':
                self.ddf, self.regularization_loss, \
                    self.scores = create_ddf_score_net(self.augmented_data['target_image'],
                                                       self.augmented_data['atlases_image'],
                                                       dropout_rate=self.dropout_rate,
                                                       n_atlas=self.n_atlas,
                                                       train_phase=self.train_phase,
                                                       regularizer=self.net_regularizer,
                                                       **self.net_kwargs)

            else:
                raise ValueError("Unknown method: %s" % self.net_kwargs['method'])
        
        # integrate velocity fields by scaling and squaring
        if self.net_kwargs['diffeomorphism']:
            self.int_steps = self.net_kwargs.pop('int_steps', 8)
            self.vec = self.ddf / (2**self.int_steps)
            self.ddf = utils_2d.integrate_vec(self.vec, self.int_steps)
        
        with tf.variable_scope('loss'):
            # get target probability map, sigma is set to self.prob_sigma[0] (the finest scale) as default
            self.target_prob = utils_2d.get_prob_from_label(self.augmented_data['target_label'],
                                                            sigma=self.prob_sigma[0],
                                                            eps=self.prob_eps)


            # get warped atlases probs/labels from each scale of ddf, each of shape [n_batch, *vol_shape, n_atlas, n_class]
            self.warped_atlases_prob = self._get_warped_atlases_prob(self.augmented_data['atlases_label'],
                                                                     self.ddf, interp_method='linear')

            # get warped atlases weight, of shape [n_batch, *vol_shape, n_atlas, n_class]
            self.warped_atlases_weight = self._get_warped_atlases(self.augmented_data['atlases_weight'],
                                                                  self.ddf, interp_method='linear')

            # get warped atlases joint probability map of each scale, of shape [n_batch, *vol_shape, n_class]
            self.atlases_joint_prob = [utils_2d.get_joint_prob(prob) for prob in self.warped_atlases_prob]

            # get loss function and joint distributions
            self.cost = self._get_cost(self.regularizer_type)
            self.pretrain_cost = self._get_pretrain_cost()

        # get segmentation
        self.segmenter = utils_2d.get_segmentation(utils_2d.get_joint_prob(self.warped_atlases_prob[0]
                                                                           * self.warped_atlases_weight
                                                                           )
                                                   )

        # get variables and update-ops
        self.trainable_variables = tf.trainable_variables(scope='network')
        self.training_variables = tf.global_variables(scope='network')
        self.update_ops = tf.compat.v1.get_collection(tf.GraphKeys.UPDATE_OPS, scope='network')

        # set global step and moving average
        self.global_step = tf.Variable(0, dtype=tf.int32, trainable=False, name='global_step')
        # self.ema = tf.train.ExponentialMovingAverage(decay=0.9999, num_updates=self.global_step)
        self.variables_to_restore = self.training_variables

        # get gradients
        self.gradients_node = tf.gradients(self.cost, self.trainable_variables, name='gradients')

        with tf.name_scope('metrics'):
            '''
            self.correct_pred = [tf.equal(tf.argmax(pred, -1), tf.argmax(self.target_labels, -1))
                                 for pred in self.predictor]
            self.acc, self.update_acc = zip(*[tf.metrics.accuracy(tf.argmax(self.target_labels, -1),
                                                                  tf.argmax(pred, -1),
                                                                  name='acc') for pred in self.predictor])
            self.sens, self.update_sens = zip(*[tf.metrics.sensitivity_at_specificity(self.target_labels[..., 1:],
                                                                                      pred[..., 1:],
                                                                                      0.95, num_thresholds=50,
                                                                                      name='sens')
                                                for pred in self.predictor])
            self.spec, self.update_spec = zip(*[tf.metrics.specificity_at_sensitivity(self.target_labels[..., 1:],
                                                                                      pred[..., 1:],
                                                                                      0.95, num_thresholds=50,
                                                                                      name='spec')
                                                for pred in self.predictor])
            self.auc, self.update_auc = zip(*[tf.metrics.auc(self.target_labels[..., 1:],
                                                             pred[..., 1:],
                                                             num_thresholds=50,
                                                             name='auc') for pred in self.predictor])
            '''
            self.average_dice = OverlapMetrics(n_class).averaged_foreground_dice(self.augmented_data['target_label'],
                                                                                 self.segmenter)
            self.myocardial_dice = OverlapMetrics(n_class).class_specific_dice(self.augmented_data['target_label'],
                                                                               self.segmenter, i=1)
            self.jaccard = OverlapMetrics(n_class).averaged_foreground_jaccard(self.augmented_data['target_label'],
                                                                               self.segmenter)
            self.ddfs_norm = tf.reduce_mean(tf.norm(self.ddf, axis=[1, 2]), name='ddfs_norm')

    def _get_augmented_data(self, type=''):
        """
        Data augmentation using affine transformations.
        :param type: type of augmentation
        :return: The augmented data in training stage, whereas the original data in validation/test stage.
        """
        with tf.name_scope('augment_data'):
            def true_fn():
                augmented_data = dict(zip(['target_image', 'target_label', 'target_weight'],
                                          random_affine_augment([self.data['target_image'], self.data['target_label'],
                                                                 self.data['target_weight']],
                                                                interp_methods=['linear', 'nearest', 'linear'],
                                                                **self.aug_kwargs)))
                # augmented_data.update(dict(zip(['atlases_image', 'atlases_label'], random_affine_augment([
                # self.data['atlases_image'], self.data['atlases_label']], interp_methods=['linear', 'nearest'],
                # **self.aug_kwargs))))
                augmented_data.update(dict(zip(['atlases_image', 'atlases_label', 'atlases_weight'],
                                               [self.data['atlases_image'], self.data['atlases_label'],
                                                self.data['atlases_weight']])))
                return augmented_data

            return tf.cond(self.train_phase, true_fn, lambda: self.data)

    def _get_pretrain_cost(self):
        with tf.name_scope('pretrain_cost'):
            return tf.reduce_mean(self.ddf ** 2, name='pretrain_cost')

    def _get_cost(self, regularizer_type=None):
        """
        Constructs the cost function, Optional arguments are:
        regularization_coefficient: weight of the regularization term

        :param regularizer_type: type of regularization

        :return: loss - The weighted sum of the negative log-likelihood and the regularization term, as well as the
            auxiliary guidance term if designated;
                 joint_probs - The joint distribution of images and tissue classes, of shape [n_batch, *vol_shape,
            n_class].
        """

        with tf.name_scope('cost_function'):
            if self.cost_name == 'mvmm':
                raise NotImplementedError

            elif self.cost_name == 'mvmm_mas':
                raise NotImplementedError

            elif self.cost_name == 'label_consistency':
                loss = LabelConsistencyLoss(**self.cost_kwargs).loss(self.target_prob,
                                                                     self.atlases_joint_prob[0],
                                                                     self.pi)

            elif self.cost_name == 'multi_scale_label_consistency':
                loss = LabelConsistencyLoss(**self.cost_kwargs).multi_scale_loss(self.target_prob,
                                                                                 self.atlases_joint_prob,
                                                                                 self.pi)

            elif self.cost_name == 'dice':
                # Dice loss between two probabilistic labels
                Dice = DiceLoss()
                loss = tf.reduce_mean(tf.stack([Dice.loss(self.target_prob,
                                                          self.warped_atlases_prob[0][..., i, :])
                                                for i in range(self.n_atlas)]))

            elif self.cost_name == 'multi_scale_dice':
                loss = DiceLoss().multi_scale_loss(self.target_prob, self.atlases_joint_prob)

            elif self.cost_name == 'cross_entropy':
                # class conditional probabilities over all atlases, of shape [n_batch, *vol_shape, n_class]
                loss = tf.reduce_mean(
                    tf.nn.softmax_cross_entropy_with_logits_v2(labels=self.augmented_data['target_label'],
                                                               logits=self.atlases_joint_prob[0]),
                    name='cross_entropy')

            elif self.cost_name == 'SSD':
                loss = tf.reduce_mean(tf.square(self.target_prob - self.atlases_joint_prob[0]), name='SSD')

            elif self.cost_name == 'LNCC':
                warped_atlases_image = self._get_warped_atlases(self.augmented_data['atlases_image'], self.ddf)
                loss = tf.reduce_mean(tf.stack([CrossCorrelation().loss(self.augmented_data['target_image'],
                                                                        warped_atlases_image[..., i, :])
                                                for i in range(self.n_atlas)]))

            elif self.cost_name == 'KL_divergence':
                raise NotImplementedError

            elif self.cost_name == 'L2_norm':
                loss = tf.reduce_mean(tf.square(self.ddf))

            elif self.cost_name == 'mvmm_net_gmm':
                loss = MvMMNetLoss(**self.cost_kwargs).loss_weight(self.target_prob,
                                                                   self.warped_atlases_prob[0],
                                                                   self.augmented_data['target_weight'],
                                                                   self.warped_atlases_weight,
                                                                   self.pi)

            elif self.cost_name == 'mvmm_net_ncc':
                loss = MvMMNetLoss(**self.cost_kwargs).loss_weight(self.target_prob,
                                                                   self.warped_atlases_prob[0],
                                                                   self.augmented_data['target_weight'],
                                                                   self.warped_atlases_weight,
                                                                   self.pi)

            elif self.cost_name == 'mvmm_net_lecc':
                loss = MvMMNetLoss(**self.cost_kwargs).loss_weight(self.target_prob,
                                                                   self.warped_atlases_prob[0],
                                                                   self.augmented_data['target_weight'],
                                                                   self.warped_atlases_weight,
                                                                   self.pi)

            elif self.cost_name == 'mvmm_net_mask':
                loss = MvMMNetLoss(**self.cost_kwargs).loss_mask(self.target_prob, self.warped_atlases_prob[0], self.pi)
            else:
                raise NotImplementedError

            if self.net_kwargs['method'] == 'ddf_score':
                Jaccard = OverlapMetrics(n_class=self.n_class, one_hot=False, reduce_mean=False)
                warped_atlases_jaccard = tf.stack([Jaccard.averaged_foreground_jaccard(self.augmented_data['target_label'],
                                                                                       self.warped_atlases_prob[0][..., i, :])
                                                   for i in range(self.n_atlas)], axis=-1)  # [n_batch, n_atlas]
                CE = CrossEntropy(eps=0.01, reduce_mean=False)
                warped_atlases_CE = tf.stack([tf.reduce_mean(tf.pow(x=CE.loss(y_true=self.augmented_data['target_label'],
                                                                              y_pred=1-self.warped_atlases_prob[0][..., i, :]),
                                                                    y=0.3),
                                                             axis=(1, 2))
                                              for i in range(self.n_atlas)], axis=-1)
                self.scores_gt = warped_atlases_jaccard + warped_atlases_CE

                self.scores_loss = tf.reduce_mean(tf.abs(tf.squeeze(self.scores, axis=-1)-self.scores_gt))
                loss += self.scores_loss
            else:
                self.scores_loss = self.scores_gt = tf.constant(0, dtype=tf.float32)

            # add regularization loss
            if regularizer_type[0] in ('l2', 'l1') and self.regularization_coefficient[0]:
                loss += self.regularization_loss * self.regularization_coefficient[0]

            return loss

    def _get_warped_atlases_prob(self, atlases_label, ddf, interp_method='linear', **kwargs):
        """
        Warp multiple atlases with the dense displacement fields as the network outputs and produce warped atlases
        probability maps.

        :param atlases_label: The atlases label of shape [n_batch, *vol_shape, n_atlas, n_class].
        :param ddf: The dense displacement fields as the network outputs, of shape [n_batch, *vol_shape, n_atlas, 2],
            can be a list of multi-level ddf
        :param interp_method: The interpolation method, should be either 'linear' or 'nearest'.
        :param kwargs: optional parameters, e.g. eps to clip value
        :return: The warped atlases probs, a dictionary of tensors of shape [n_batch, *vol_shape, n_atlas, n_class],
            with each key-value pair denoting a probability map of a certain scale.
        """
        with tf.name_scope('warp_atlases_prob'):
            spatial_transform = SpatialTransformer(interp_method, name='warp_atlases_prob')
            eps = kwargs.pop("eps", self.prob_eps)

            warped_atlases_probs = []
            for idx in range(len(self.prob_sigma)):
                warped_atlases_probs.append(
                    tf.stack([spatial_transform([utils_2d.get_prob_from_label(atlases_label[..., n, :],
                                                                              self.prob_sigma[idx],
                                                                              eps=eps),
                                                 ddf[..., n, :]]) for n in range(self.n_atlas)], axis=-2))

            return warped_atlases_probs

    def _get_warped_atlases(self, atlases, ddf, **kwargs):
        with tf.name_scope('warp_atlases'):
            spatial_transform = SpatialTransformer(name='warp_atlases', **kwargs)
            warped_atlases = tf.stack([spatial_transform([atlases[..., i, :], ddf[..., i, :]])
                                       for i in range(self.n_atlas)], axis=-2)
            return warped_atlases

    def save(self, saver, sess, model_path, **kwargs):
        """
        Saves the current session to a checkpoint

        :param saver: the TensorFlow saver
        :param sess: current session
        :param model_path: path to file system location
        :param latest_filename: Optional name for the protocol buffer file that will contains the list of most recent
        checkpoints.
        """

        save_path = saver.save(sess, model_path, **kwargs)
        self.logger.info("Model saved to file: %s" % save_path)
        return save_path

    def restore(self, sess, model_path, **kwargs):
        """
        Restores a session from a checkpoint

        :param sess: current session instance
        :param model_path: path to file system checkpoint location
        """

        saver = tf.train.Saver(**kwargs)
        saver.restore(sess, model_path)
        self.logger.info("Model restored from file: %s" % model_path)

    def __str__(self):
        # Todo: to make the print more complete and pretty
        return "\n################ Network Parameter Settings ################\n" \
               "input_size= {}, num_channels= {}, num_classes= {}, num_atlases= {}, " \
               "num_subtypes= {}, \n" \
               "ddf_levels= {}, features_root= {}, dropout_rate= {}, \n" \
               "cost_name= {}, prob_sigma= {}, regularizer_type= {}, " \
               "regularizer_coefficient= {}".format(self.input_size, self.channels,
                                                    self.n_class, self.n_atlas, self.n_subtypes,
                                                    self.net_kwargs.get("ddf_levels"),
                                                    self.net_kwargs.get("features_root"),
                                                    self.net_kwargs.get("dropout_rate"),
                                                    self.cost_name, self.prob_sigma,
                                                    self.regularizer_type,
                                                    self.regularization_coefficient)

    __repr__ = __str__


class NetForPrediction(UnifiedMultiAtlasSegNet):
    """
    Model prediction for the unified multi-atlas segmentation network.
    """

    def __init__(self, input_size=(64, 64, 64), channels=1, n_class=2, n_atlas=1, n_subtypes=(2, 1,), cost_kwargs=None,
                 **net_kwargs):
        """
        :param input_scale: The input scale for the network.
        :param test_input_size: The test input size.
        :param input_size: The input size for the network.
        :param channels: (Optional) number of channels in the input target image.
        :param n_class: (Optional) number of output labels.
        :param n_atlas: The number of atlases within the multivariate mixture model.
        :param n_subtypes: A tuple indicating the number of subtypes within each tissue class, with the first element
            corresponding to the background subtypes.
        :param cost_kwargs: (Optional) kwargs passed to the cost function, e.g. regularizer_type/auxiliary_cost_name.
        """

        super(NetForPrediction, self).__init__(input_size, channels, n_class, n_atlas, n_subtypes,
                                               cost_kwargs, **net_kwargs)

    def predict_scale(self, sess, test_data, dropout_rate):
        """
        Restore the model to make inference for the test data with resized dense displacement fields.

        :param sess: The session for predictions.
        :param test_data: The test data for model inference.
        :param dropout_rate: dropout probability for network inference;
        :return: image_pred - The predicted array representing the warped atlas images;
                 label_pred - The predicted predictions representing the warped atlas labels;
                 metrics - A dictionary combining list of various evaluation metrics.
        """

        self.logger.info("Start predicting warped test atlas!")

        warped_atlases_prob, \
        warped_atlases_weight = sess.run((self.warped_atlases_prob[0],
                                          self.warped_atlases_weight),
                                         feed_dict={self.data['target_image']: test_data['target_image'],
                                                    self.data['atlases_image']: test_data['atlases_image'],
                                                    self.data['atlases_label']: test_data['atlases_label'],
                                                    self.data['atlases_weight']: test_data['atlases_weight'],
                                                    self.dropout_rate: dropout_rate,
                                                    self.train_phase: False})

        # Overlap = OverlapMetrics(n_class=self.n_class, mode='np')
        # dice = Overlap.averaged_foreground_dice(y_true=test_data['target_label'], y_seg=label_pred)
        # class_specific_dice = [Overlap.class_specific_dice(y_true=test_data['target_label'], y_seg=label_pred, i=k)
        #                        for k in range(self.n_class)]
        #
        # metrics = {'Dice': dice,
        #            'Jaccard': Overlap.averaged_foreground_jaccard(y_true=test_data["target_label"],
        #                                                           y_seg=label_pred),
        #            'Myocardial Dice': class_specific_dice[1],
        #            'LV Dice': class_specific_dice[2],
        #            'RV Dice': class_specific_dice[3]}
        #
        # self.logger.info("Metrics after registration: "
        #                  "Dice= {:.4f}, Myocardial Dice= {:.4f}, "
        #                  "LV Dice= {:.4f}, RV Dice= {:.4f}".format(metrics['Dice'], metrics['Myocardial Dice'],
        #                                                            metrics['LV Dice'], metrics['RV Dice'])
        #                  )

        # return label_pred, ddf, metrics
        return warped_atlases_prob, warped_atlases_weight


class Trainer(object):
    """
    Trains a unified multi-atlas segmentation network.
    """

    def __init__(self, net, batch_size=1, norm_grads=False, optimizer_name="momentum", learning_rate=0.001,
                 num_workers=0, opt_kwargs=None):
        """
        :param net: The network instance to train.
        :param batch_size: The size of training batch.
        :param norm_grads: (Optional) true if normalized gradients should be added to the summaries.
        :param optimizer_name: (Optional) name of the optimizer to use (momentum or adam).
        :param learning_rate: learning rate
        :param num_workers: How many sub-processes to use for data loading.
            0 means that the data will be loaded in the main process. (default: 0)
        :param opt_kwargs: (Optional) kwargs passed to the learning rate (momentum opt) and to the optimizer.
        """
        if opt_kwargs is None:
            opt_kwargs = {}
        self.net = net
        self.batch_size = batch_size
        self.norm_grads = norm_grads
        self.optimizer_name = optimizer_name
        self.num_workers = num_workers
        self.opt_kwargs = opt_kwargs
        self.learning_rate = learning_rate

    def _get_optimizer(self, cost, global_step, clip_gradient=False, **kwargs):
        optimizer_name = kwargs.pop('optimizer', self.optimizer_name)
        decay_steps = kwargs.pop('decay_step', 100000)
        trainable_variables = self.net.trainable_variables

        # variables_to_average = trainable_variables + tf.moving_average_variables()
        # ema = self.net.ema
        self.decay_rate = self.opt_kwargs.get("decay_rate", 0.999)
        if optimizer_name == "momentum":
            init_lr = kwargs.pop("lr", 0.2)
            momentum = self.opt_kwargs.get("momentum", 0.9)
            self.net.logger.info("SGD optimizer with initial lr: {:.2e}, momentum: {:.2f}, "
                                 "decay steps: {:d}, decay rate: {:.2f}".format(init_lr, momentum, decay_steps,
                                                                                self.decay_rate))
            learning_rate_node = tf.train.exponential_decay(learning_rate=init_lr,
                                                            global_step=global_step,
                                                            decay_steps=decay_steps,
                                                            decay_rate=self.decay_rate,
                                                            staircase=True, name='learning_rate')
            optimizer = tf.train.MomentumOptimizer(learning_rate=learning_rate_node, momentum=momentum,
                                                   **self.opt_kwargs)

        elif optimizer_name == "sgd":
            init_lr = kwargs.pop('lr', 0.1)
            self.net.logger.info("SGD optimizer with initial lr: {:.2e}, "
                                 "decay steps: {:d}, decay rate: {:.2f}".format(init_lr, decay_steps,
                                                                                self.decay_rate))
            learning_rate_node = tf.train.exponential_decay(learning_rate=init_lr,
                                                            global_step=global_step,
                                                            decay_steps=decay_steps,
                                                            decay_rate=self.decay_rate,
                                                            staircase=True, name='learning_rate')
            optimizer = tf.train.GradientDescentOptimizer(learning_rate=learning_rate_node, **self.opt_kwargs)

        elif optimizer_name == 'rmsprop':
            init_lr = kwargs.pop('lr', 0.001)
            self.net.logger.info("RMSprop optimizer with initial lr: {:.2e}".format(init_lr))
            learning_rate_node = tf.Variable(init_lr, trainable=False, name='learning_rate')
            optimizer = tf.train.RMSPropOptimizer(learning_rate=learning_rate_node, **self.opt_kwargs)

        elif optimizer_name == "adam":
            init_lr = kwargs.pop('lr', 0.001)
            self.net.logger.info("Adam optimizer with initial lr: {:.2e}".format(init_lr))
            learning_rate_node = tf.Variable(init_lr, trainable=False, name='learning_rate')
            optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate_node, **self.opt_kwargs)

        elif optimizer_name == 'adam-clr':
            from core.clr import cyclic_learning_rate
            init_lr = kwargs.pop('lr', 0.001)
            step_size = kwargs.pop("step_size", 100000)
            gamma = kwargs.pop("gamma", 0.99999)
            self.net.logger.info("Adam optimizer with cyclic learning rate, initial lr: {:.2e} "
                                 "step_size: {:d}, gamma: {:.5e}".format(init_lr, step_size, gamma))
            learning_rate_node = cyclic_learning_rate(global_step, learning_rate=init_lr,
                                                      max_lr=init_lr * 10,
                                                      step_size=step_size, gamma=gamma,
                                                      mode='exp_range')
            optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate_node, **self.opt_kwargs)

        elif optimizer_name == 'radam':
            raise NotImplementedError

        elif optimizer_name == 'adabound':
            raise NotImplementedError

        else:
            raise ValueError("Unknown optimizer: %s" % optimizer_name)

        if clip_gradient:
            gradients, variables = zip(*optimizer.compute_gradients(cost, var_list=trainable_variables))

            # clip by global norm
            capped_grads, _ = tf.clip_by_global_norm(gradients, 1.0)
            '''
            # clip by individual norm
            capped_grads = [None if grad is None else tf.clip_by_norm(grad, 1.0) for grad in gradients]
            '''
            opt_op = optimizer.apply_gradients(zip(capped_grads, variables), global_step=global_step)
        else:
            opt_op = optimizer.minimize(cost, global_step=global_step, var_list=trainable_variables)

        train_op = tf.group([opt_op, self.net.update_ops])

        return optimizer, train_op, init_lr, learning_rate_node

    def _initialize(self, training_iters, self_iters, decay_epochs, epochs, clip_gradient, restore_model_path,
                    save_model_path, restore, prediction_path, pretrain_epochs):
        self.training_iters = training_iters
        self.self_iters = self_iters
        self.decay_epochs = decay_epochs
        self.epochs = epochs
        opt_decay_steps = training_iters * self_iters * decay_epochs

        if self.net.summaries and self.norm_grads:
            self.norm_gradients_node = tf.Variable(tf.constant(0.0, shape=[len(self.net.gradients_node)]),
                                                   name='norm_gradients')
            tf.summary.histogram('norm_grads', self.norm_gradients_node)

        # create summary protocol buffers for training metrics
        with tf.name_scope('Training_metrics_summaries'):
            tf.summary.scalar('Training_Loss', tf.reduce_mean(self.net.cost))
            #     tf.summary.scalar('Training_Accuracy', tf.reduce_mean(self.net.acc))
            #     tf.summary.scalar('Training_AUC', tf.reduce_mean(self.net.auc))
            #     tf.summary.scalar('Training_Sensitivity', tf.reduce_mean(self.net.sens))
            #     tf.summary.scalar('Training_Specificity', tf.reduce_mean(self.net.spec))
            tf.summary.scalar('Training_Average_Dice', self.net.average_dice)
            tf.summary.scalar('Training_Myocardial_Dice', self.net.myocardial_dice)
            tf.summary.scalar('Training_Jaccard', self.net.jaccard)
            tf.summary.scalar('Training_DDFs_Norm', self.net.ddfs_norm)

        # add bending energy
        if self.net.regularizer_type[1] == 'bending_energy':
            with tf.name_scope('bending_energy'):
                bending_energy_increment_rate = self.net.cost_kwargs.pop('bending_energy_increment_rate')
                bending_energy_weight = tf.train.exponential_decay(self.net.regularization_coefficient[1],
                                                                   global_step=self.net.global_step,
                                                                   decay_steps=training_iters * self_iters,
                                                                   decay_rate=bending_energy_increment_rate,
                                                                   staircase=True, name='bending_energy_weight')
                BendingEnergy = LocalDisplacementEnergy(energy_type='bending')
                bending_energy = tf.reduce_mean([BendingEnergy.compute_displacement_energy(self.net.ddf[..., i, :],
                                                                                           bending_energy_weight)
                                                 for i in range(self.net.n_atlas)])
                setattr(self.net, 'bending_energy', bending_energy)
                self.net.cost += bending_energy

                jacobian_det = tf.reduce_mean([BendingEnergy.compute_jacobian_determinant(self.net.ddf[..., i, :])
                                               for i in range(self.net.n_atlas)])
                num_neg_jacob = tf.math.count_nonzero(tf.less_equal(jacobian_det, 0), dtype=tf.float32,
                                                      name='negative_jacobians_number')
                setattr(self.net, 'num_neg_jacob', num_neg_jacob)

        # initialize optimizer
        with tf.name_scope('optimizer'):
            self.optimizer, self.train_op, \
            self.init_lr, self.learning_rate_node = self._get_optimizer(self.net.cost, self.net.global_step,
                                                                        clip_gradient, lr=self.learning_rate,
                                                                        decay_steps=opt_decay_steps,
                                                                        step_size=2 * training_iters * self_iters,
                                                                        gamma=0.99998)
            if pretrain_epochs:
                _, self.pretrain_op, \
                _, self.pretrain_lr = self._get_optimizer(self.net.pretrain_cost,
                                                          global_step=tf.Variable(0, trainable=False, dtype=tf.int32),
                                                          optimizer='adam', lr=1e-4)

        # create a summary protocol buffer for learning rate
        with tf.name_scope('lr_summary'):
            tf.summary.scalar('learning_rate', self.learning_rate_node)

        # Merges summaries in the default graph
        self.summary_op = tf.summary.merge_all()

        # create an op that initializes all training variables
        init = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

        self.prediction_path = prediction_path
        self.restore_model_path = restore_model_path
        self.save_model_path = save_model_path
        abs_prediction_path = os.path.abspath(prediction_path)
        abs_model_path = os.path.abspath(save_model_path)

        # remove the previous directory for model storing and validation prediction
        if not restore:
            self.net.logger.info("Removing '{:}'".format(abs_prediction_path))
            shutil.rmtree(abs_prediction_path, ignore_errors=True)
            self.net.logger.info("Removing '{:}'".format(abs_model_path))
            shutil.rmtree(abs_model_path, ignore_errors=True)

        # create a new directory for model storing and validation prediction
        if not os.path.exists(abs_prediction_path):
            self.net.logger.info("Allocating '{:}'".format(abs_prediction_path))
            os.makedirs(abs_prediction_path)

        if not os.path.exists(abs_model_path):
            self.net.logger.info("Allocating '{:}'".format(abs_model_path))
            os.makedirs(abs_model_path)

        return init

    def train(self, train_data_provider, test_data_provider, validation_batch_size, save_model_path, pretrain_epochs=5,
              epochs=100, dropout=0.2, clip_gradient=False, display_step=1, self_iters=1, decay_epochs=1,
              restore=False, write_graph=False, prediction_path='validation_prediction', restore_model_path=None,
              **kwargs):
        """
        Launch the training process.

        :param train_data_provider: Callable returning training data.
        :param test_data_provider: Callable returning validation data.
        :param validation_batch_size: The number of data for validation.
        :param save_model_path: The path where to store checkpoints.
        :param pretrain_epochs: Number of pre-training epochs.
        :param epochs: The number of epochs.
        :param dropout: The dropout probability.
        :param clip_gradient: Whether to apply gradient clipping.
        :param display_step: The number of steps till outputting stats.
        :param self_iters: The number of self iterations.
        :param decay_epochs: The number of epochs for learning rate decay.
        :param restore: Flag if previous model should be restored.
        :param restore_model_path: Where to restore the previous model.
        :param write_graph: Flag if the computation graph should be written as proto-buf file to the output path.
        :param prediction_path: The path where to save predictions on each epoch.
        """
        saver = tf.train.Saver(var_list=self.net.variables_to_restore, max_to_keep=kwargs.pop('max_to_keep', 5))
        best_saver = tf.train.Saver(var_list=self.net.variables_to_restore)
        save_path = os.path.join(save_model_path, "best_model.ckpt")
        # moving_average_path = os.path.join(save_model_path, "moving_average_model.ckpt")

        # initialize data loader
        train_data_loader = DataLoader(train_data_provider, batch_size=self.batch_size, shuffle=True,
                                       num_workers=self.num_workers, collate_fn=train_data_provider.collate_fn)
        # set default training iterations for every epoch and validation step
        training_iters = len(train_data_loader)
        validation_step = kwargs.pop('validation_step', None)
        if validation_step is None:
            validation_step = training_iters
        self.net.logger.info("Number of training iteration each epoch: %s" % training_iters)

        # initialize optimizer
        init = self._initialize(training_iters, self_iters, decay_epochs, epochs,
                                clip_gradient, restore_model_path, save_model_path,
                                restore, prediction_path, pretrain_epochs)

        with tf.Session(config=config) as sess:
            if write_graph:
                tf.train.write_graph(sess.graph_def, save_model_path, "graph.pb", False)

            # initialize variables
            sess.run(init)

            # pre-training
            if pretrain_epochs:
                pretrain_steps = kwargs.pop('pretrain_steps', None)
                if pretrain_steps is None:
                    pretrain_steps = pretrain_epochs * training_iters
                pretrain_saver = tf.train.Saver(var_list=self.net.variables_to_restore)
                pretrain_loss = 0.
                self.net.logger.info("Start pre-training by minimizing L2-norm of dense displacement fields......")
                for step, batch in enumerate(train_data_loader):
                    if step < pretrain_steps:
                        _, loss, pretrain_lr = sess.run((self.pretrain_op, self.net.pretrain_cost, self.pretrain_lr),
                                                        feed_dict={self.net.data['target_image']: batch['target_image'],
                                                                   self.net.data['atlases_image']: batch['atlases_image'],
                                                                   self.net.dropout_rate: dropout,
                                                                   self.net.train_phase: True})
                        pretrain_loss += loss

                        if step % display_step == 0:
                            self.net.logger.info("[Pre-training] Step: %d, "
                                                 "Pre-training loss: %.4f" % (step, loss))
                    else:
                        break

                self.net.logger.info("[Pre-training] "
                                     "Average pre-training loss: %.4f, "
                                     "Learning rate: %.2e" % (pretrain_loss / pretrain_steps,
                                                              pretrain_lr)
                                     )
                self.net.save(pretrain_saver, sess, os.path.join(save_model_path, 'pretrain_model.ckpt'),
                              latest_filename='pretrain_checkpoint')
                self.net.logger.info("Finish network pre-training!")

            # restore variables
            if restore:
                self.net.logger.info("Restoring from model path: %s" % restore_model_path)
                if '.ckpt' in restore_model_path:
                    self.net.logger.info("Restoring checkpoint: %s" % restore_model_path)
                    new_saver = tf.train.import_meta_graph(restore_model_path)
                    new_saver.restore(sess, restore_model_path, var_list=self.net.variables_to_restore)
                else:
                    ckpt = tf.train.get_checkpoint_state(restore_model_path,
                                                         latest_filename=kwargs.pop('latest_filename', None))
                    if ckpt and ckpt.model_checkpoint_path:
                        self.net.logger.info("Restoring checkpoint: %s" % ckpt.model_checkpoint_path)
                        self.net.restore(sess, ckpt.model_checkpoint_path, var_list=self.net.variables_to_restore)
                    else:
                        ckpt = tf.train.get_checkpoint_state(save_model_path,
                                                             latest_filename=kwargs.pop('latest_filename', None))
                        if ckpt and ckpt.model_checkpoint_path:
                            self.net.logger.info("Restoring checkpoint: %s" % ckpt.model_checkpoint_path)
                            self.net.restore(sess, ckpt.model_checkpoint_path, var_list=self.net.variables_to_restore)
                        else:
                            raise ValueError("Unknown previous model path: " % ckpt.model_checkpoint_path)

            # create summary writer for training summaries
            summary_writer = tf.summary.FileWriter(save_model_path, graph=sess.graph)

            # create dictionary to record training/validation metrics for visualization
            test_metrics = {"Loss": {}, "Dice": {}, "Jaccard": {}, "Myocardial Dice": {},
                            "DDFs norm": {}, "Bending energy": {}, "# Negative Jacobians": {}}
            train_metrics = {"Loss": {}, "Dice": {}, "Jaccard": {}, "Myocardial Dice": {},
                             "DDFs norm": {}, "Bending energy": {}, "# Negative Jacobians": {}}

            if epochs == 0:
                return save_path, train_metrics, test_metrics

            self.net.logger.info(
                "Start Unified Multi-Atlas Seg-Net optimization based on loss function: {}, prob_sigma: {} "
                "regularizer type: {} with regularization coefficient: {}, optimizer type: {}, "
                "batch size: {}, initial learning rate: {:.2e}".format(self.net.cost_name,
                                                                       self.net.prob_sigma,
                                                                       self.net.regularizer_type,
                                                                       self.net.regularization_coefficient,
                                                                       self.optimizer_name, self.batch_size,
                                                                       self.init_lr))

            lr = 0.
            assert self_iters >= 1
            total_loss = 0.
            for epoch in range(epochs):
                for step, batch in enumerate(train_data_loader):
                    # get validation metrics
                    if step % validation_step == 0:
                        epoch_test_metrics = self.store_prediction(sess, test_data_provider, validation_batch_size,
                                                                   dropout_rate=dropout,
                                                                   save_dir='epoch%s_step%s' % (epoch, step))
                        # save the current model if it is the best one hitherto
                        if (step > 0 or epoch > 0) and epoch_test_metrics['Dice'] >= np.max(list(test_metrics['Dice'].values())):
                            save_path = self.net.save(best_saver, sess, save_path, latest_filename='best_checkpoint')

                        # record epoch validation metrics
                        for k, v in test_metrics.items():
                            v[epoch * training_iters * self_iters + step * self_iters] = epoch_test_metrics[k]

                        # visualise training and validation metrics
                        utils_2d.visualise_metrics([train_metrics, test_metrics],
                                                   save_path=os.path.dirname(self.prediction_path),
                                                   labels=['training', 'validation'])

                    # optimization operation (back-propagation)
                    for self_step in range(self_iters):
                        _, loss = sess.run((self.train_op, self.net.cost),
                                           feed_dict={self.net.data['target_image']: batch['target_image'],
                                                      self.net.data['target_label']: batch['target_label'],
                                                      self.net.data['target_weight']: batch['target_weight'],
                                                      self.net.data['atlases_label']: batch['atlases_label'],
                                                      self.net.data['atlases_image']: batch['atlases_image'],
                                                      self.net.data['atlases_weight']: batch['atlases_weight'],
                                                      self.net.dropout_rate: dropout,
                                                      self.net.train_phase: True})
                        total_loss += loss

                    # display mini-batch statistics and record training metrics
                    if step % display_step == 0:
                        # get training metrics for the display step
                        step_train_metrics, grads, lr = self.output_minibatch_stats(sess, summary_writer, epoch, step,
                                                                                    batch, dropout_rate=dropout)
                        # record training losses
                        for k, v in train_metrics.items():
                            v[epoch * training_iters * self_iters + (step + 1) * self_iters] = step_train_metrics[k]

                # display epoch statistics
                self.output_epoch_stats(epoch, total_loss, (epoch + 1) * training_iters * self_iters, lr)

                # save the current model
                self.net.save(saver, sess, os.path.join(save_model_path, 'epoch%s_model.ckpt' % epoch),
                              global_step=(epoch + 1) * training_iters * self_iters)
                # self.net.save(sess, moving_average_path, latest_filename='moving_average_checkpoint')

            self.net.logger.info("Optimization Finished!")
            self.net.save(saver, sess, os.path.join(save_model_path, 'checkpoint.ckpt'))

            return save_path, train_metrics, test_metrics

    def store_prediction(self, sess, test_data_provider, validation_batch_size, dropout_rate, **kwargs):
        """
        Compute validation metrics and store visualization results.

        :param sess: The pre-defined session for running TF operations.
        :param test_data_provider: The test data-provider.
        :param validation_batch_size: The validation data size.
        :param dropout_rate: The dropout probability.
        :param save_prefix: The save prefix for results saving.
        :return: A dictionary containing validation metrics.
        """
        save_prefix = kwargs.pop('save_prefix', '')
        save_dir = kwargs.pop('save_dir', '')
        sess.run(tf.local_variables_initializer())
        if validation_batch_size is not None:
            # randomly sample the validation data at each epoch
            data_indices = random.sample(range(len(test_data_provider)), validation_batch_size)
        else:
            validation_batch_size = len(test_data_provider)
            data_indices = range(validation_batch_size)
        loss = np.zeros([validation_batch_size])
        dice = np.zeros([validation_batch_size])
        jaccard = np.zeros([validation_batch_size])
        myo_dice = np.zeros([validation_batch_size])
        ddfs_norm = np.zeros([validation_batch_size])
        bending_energy = np.zeros([validation_batch_size])
        num_neg_jacob = np.zeros([validation_batch_size])
        scores_loss = np.zeros([validation_batch_size])
        for i in range(validation_batch_size):
            data = test_data_provider[data_indices[i]]
            loss[i], dice[i], jaccard[i], myo_dice[i], ddfs_norm[i], bending_energy[i], \
                num_neg_jacob[i], scores_loss[i], \
                test_pred, ddf = sess.run((self.net.cost, self.net.average_dice, self.net.jaccard,
                                            self.net.myocardial_dice, self.net.ddfs_norm,
                                            self.net.bending_energy, self.net.num_neg_jacob, self.net.scores_loss,
                                            self.net.segmenter, self.net.ddf),
                                           feed_dict={self.net.data['target_image']: data['target_image'],
                                                      self.net.data['target_label']: data['target_label'],
                                                      self.net.data['target_weight']: data['target_weight'],
                                                      self.net.data['atlases_label']: data['atlases_label'],
                                                      self.net.data['atlases_image']: data['atlases_image'],
                                                      self.net.data['atlases_weight']: data['atlases_weight'],
                                                      # self.net.pi: test_pi[i],
                                                      self.net.train_phase: False,
                                                      self.net.dropout_rate: dropout_rate})

            utils_2d.save_prediction_png(data['target_image'], data['target_label'], test_pred,
                                         os.path.join(self.prediction_path, save_dir), name_index=data_indices[i],
                                         data_provider=test_data_provider, save_prefix=save_prefix)

            # utils.save_prediction_nii(test_pred.squeeze(0), self.prediction_path, test_data_provider,
            #                           name_index=data_indices[i], data_type='label', affine=data['target_affine'],
            #                           header=data['target_header'], save_prefix=save_prefix)

            target_name, atlases_name = test_data_provider.get_image_names(data_indices[i])
            for k in range(self.net.n_atlas):
                t_name = '_'.join(os.path.basename(target_name).split('_')[0:3])
                a_name = '_'.join(os.path.basename(atlases_name[k]).split('_')[0:3])
                save_name = 'target-' + t_name + '_atlas-' + a_name
                utils_2d.save_prediction_nii(ddf[0, ..., k, :], os.path.join(self.prediction_path, save_dir),
                                             test_data_provider, save_name=save_name, data_type='vector_fields',
                                             save_prefix=save_prefix)

        '''
        acc, auc, sens, spec = sess.run([self.net.acc[0], self.net.auc[0], self.net.sens[0], self.net.spec[0]])
        '''
        metrics = {'Loss': np.mean(loss), 'DDFs norm': np.mean(ddfs_norm), 'Dice': np.mean(dice),
                   'Jaccard': np.mean(jaccard), 'Myocardial Dice': np.mean(myo_dice),
                   'Bending energy': np.mean(bending_energy), '# Negative Jacobians': np.mean(num_neg_jacob)}

        self.net.logger.info("[Validation] Loss= {:.4f}, Scores loss={:.4f}, DDFs norm= {:.4f}, "
                             "Bending energy= {:.4f}, # Negative Jacobians= {:.1f}, "
                             "Dice= {:.4f}, Jaccard= {:.4f}, "
                             "Myocardial Dice= {:.4f}".format(metrics['Loss'],
                                                              np.mean(scores_loss),
                                                              metrics['DDFs norm'],
                                                              metrics['Bending energy'],
                                                              metrics['# Negative Jacobians'],
                                                              metrics['Dice'], metrics['Jaccard'],
                                                              metrics['Myocardial Dice'])
                             )

        return metrics

    def output_epoch_stats(self, epoch, total_loss, training_iters, lr):
        self.net.logger.info(
            "[Training] Epoch {:}, Average Loss: {:.4f}, "
            "learning rate: {:.1e}".format(epoch, (total_loss / training_iters), lr))

    def output_minibatch_stats(self, sess, summary_writer, epoch, step, batch, dropout_rate):
        # Calculate batch loss and metrics
        sess.run(tf.local_variables_initializer())

        summary_str, \
        loss, lr, grads, \
        dice, jaccard, myo_dice, \
        ddfs_norm, bending_energy, scores_loss, \
        num_neg_jacob = sess.run((self.summary_op, self.net.cost, self.learning_rate_node, self.net.gradients_node,
                                  self.net.average_dice, self.net.jaccard, self.net.myocardial_dice,
                                  self.net.ddfs_norm, self.net.bending_energy,
                                  self.net.scores_loss, self.net.num_neg_jacob),
                                 feed_dict={self.net.data['target_image']: batch['target_image'],
                                            self.net.data['target_label']: batch['target_label'],
                                            self.net.data['target_weight']: batch['target_weight'],
                                            self.net.data['atlases_label']: batch['atlases_label'],
                                            self.net.data['atlases_image']: batch['atlases_image'],
                                            self.net.data['atlases_weight']: batch['atlases_weight'],
                                            # self.net.pi: batch_pi,
                                            self.net.train_phase: False,
                                            self.net.dropout_rate: dropout_rate})
        summary_writer.add_summary(summary_str, step)
        summary_writer.flush()

        metrics = {'Loss': loss, 'DDFs norm': ddfs_norm, 'Dice': dice, 'Jaccard': jaccard,
                   'Myocardial Dice': myo_dice, "Bending energy": bending_energy,
                   "# Negative Jacobians": num_neg_jacob,
                   'Average gradient norm': np.mean([np.linalg.norm(g) for g in grads])}

        self.net.logger.info("[Training] Epoch {:}, Iteration {:}, Mini-batch Loss= {:.4f}, "
                             "Mini-batch Scores loss= {:.4f}, "
                             "Learning rate= {:.3e}, DDFs norm= {:.4f}, Bending energy= {:.4f}, "
                             "# Negative Jacobians= {:.1f}, Average gradient norm {:.4f}, "
                             "Average foreground Dice= {:.4f}, "
                             "Myocardial Dice= {:.4f}".format(epoch, step, metrics['Loss'],
                                                              scores_loss,
                                                              lr, metrics['DDFs norm'],
                                                              metrics['Bending energy'],
                                                              metrics['# Negative Jacobians'],
                                                              metrics['Average gradient norm'],
                                                              metrics['Dice'], metrics['Myocardial Dice'])
                             )

        return metrics, grads, lr

    def __str__(self):
        # Todo: to make the print more complete
        return str(self.net) + '\n' \
                               "################ Training Setups ################\n" \
                               "batch_size= {}, optimizer_name= {}, num_workers= {}, \n" \
                               "initial learning_rate= {:.2f}, decay_rate= {:.2f}, \n" \
                               "restore_model_path= {}, saved_model_path= {}, prediction_path= {}, \n" \
                               "training_iters= {}, self_iters= {}, " \
                               "decay_epochs= {}".format(self.batch_size, self.optimizer_name,
                                                         self.num_workers, self.init_lr, self.decay_rate,
                                                         self.restore_model_path, self.save_model_path,
                                                         self.prediction_path, self.training_iters,
                                                         self.self_iters, self.decay_epochs)

    __repr__ = __str__


#####################################################################
# Helper functions
#####################################################################

def _compute_gradients(tensor, var_list):
    grads = tf.gradients(tensor, var_list)
    return [grad if grad is not None else tf.zeros_like(var)
            for var, grad in zip(var_list, grads)]