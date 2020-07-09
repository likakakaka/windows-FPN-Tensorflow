import tensorflow as tf
import tensorflow.contrib.slim as slim

from libs.configs import cfgs
from libs.box_utils import iou
from libs.box_utils import encode_and_decode
from libs.box_utils import boxes_utils
from libs.losses import losses
from libs.make_anchors import make_anchors




class RPN(object):
    def __init__(self,
                 feature_pyramid,
                 image_height,
                 image_width,
                 gtboxes_and_label,   # [batch_size, -1, 5]
                 is_training
                 ):

        self.feature_pyramid = feature_pyramid
        self.image_height = image_height
        self.image_width = image_width
        self.gtboxes_and_label = gtboxes_and_label
        self.is_training = is_training


        self.anchor_ratios = cfgs.ANCHOR_RATIOS
        self.anchor_scales = cfgs.ANCHOR_SCALES
        self.base_anchor_size_list = cfgs.BASE_ANCHOR_SIZE_LIST
        self.stride = cfgs.STRIDE

        self.num_of_anchors_per_location = len(cfgs.ANCHOR_RATIOS) * len(cfgs.ANCHOR_SCALES)
        self.rpn_encode_boxes, self.rpn_scores = self.rpn_net()



    def rpn_net(self):
        """
        base the anchor stride to compute the output(batch_size, object_scores, pred_bbox) of every feature map,
        now it supported the multi image in a gpu
        return
        rpn_all_encode_boxes(batch_size, all_anchors, 4)
        rpn_all_boxes_scores(batch_size, all_anchors, 2)
        Be Cautious:
        all_anchors is concat by the order of ［P2, P3, P4, P5, P6］ which request that
        the anchors must be generated by this order.
        """

        rpn_encode_boxes_list = []
        rpn_scores_list = []
        with tf.variable_scope('rpn_net'):
            with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(cfgs.WEIGHT_DECAY[cfgs.NET_NAME])):
                for level in cfgs.LEVEL:
                    if cfgs.SHARE_HEAD:
                        reuse_flag = None if level == 'P2' else True
                        scope_list = ['conv2d_3x3', 'rpn_classifier', 'rpn_regressor']
                        # in the begining, we should create variables, then sharing variables in P3, P4, P5
                    else:
                        reuse_flag = None
                        scope_list = ['conv2d_3x3_' + level, 'rpn_classifier_' + level, 'rpn_regressor_' + level]

                    rpn_conv2d_3x3 = slim.conv2d(inputs=self.feature_pyramid[level],
                                                 num_outputs=512,
                                                 kernel_size=[3, 3],
                                                 stride=1,
                                                 scope=scope_list[0],
                                                 reuse=reuse_flag,trainable=self.is_training)
                    rpn_box_scores = slim.conv2d(rpn_conv2d_3x3,
                                                 num_outputs=2 * self.num_of_anchors_per_location,
                                                 kernel_size=[1, 1],
                                                 stride=1,
                                                 scope=scope_list[1],
                                                 activation_fn=None,
                                                 reuse=reuse_flag,trainable=self.is_training)
                    rpn_encode_boxes = slim.conv2d(rpn_conv2d_3x3,
                                                   num_outputs=4 * self.num_of_anchors_per_location,
                                                   kernel_size=[1, 1],
                                                   stride=1,
                                                   scope=scope_list[2],
                                                   activation_fn=None,
                                                   reuse=reuse_flag,trainable=self.is_training)

                    rpn_box_scores = tf.reshape(rpn_box_scores, [cfgs.BATCH_SIZE,-1, 2])
                    rpn_encode_boxes = tf.reshape(rpn_encode_boxes, [cfgs.BATCH_SIZE,-1, 4])

                    rpn_scores_list.append(rpn_box_scores)
                    rpn_encode_boxes_list.append(rpn_encode_boxes)

                rpn_all_encode_boxes = tf.concat(rpn_encode_boxes_list, axis=1)
                rpn_all_boxes_scores = tf.concat(rpn_scores_list, axis=1)

            return rpn_all_encode_boxes, rpn_all_boxes_scores


    def build_rpn_train_sample(self):
        # anchors: all_anchors [-1,4]

        anchors = self.make_all_anchors()
        with tf.name_scope('build_rpn_train_sample'):
            def build_slice_rpn_target(gtboxes_and_label, anchors):  # 计算一张图片的情况

                """
                assign anchors targets: object or background.
                :param anchors: (all_anchors, 4)[y1, x1, y2, x2]. use N to represent all_anchors
                :param gt_boxes: (M, 4).
                :param config: the config of making data

                :return:
                """
                with tf.variable_scope('rpn_find_positive_negative_samples'):
                    gt_boxes = tf.cast(tf.reshape(gtboxes_and_label[:, :-1], [-1, 4]), tf.float32)
                    gt_boxes, non_zeros = boxes_utils.trim_zeros_graph(gt_boxes, name="trim_gt_box")  # [M, 4]
                    anchors, _ = boxes_utils.trim_zeros_graph(anchors, name="trim_rpn_proposal_train")  # 这块需要过滤吗？
                    ious = iou.iou_calculate(anchors, gt_boxes)  # (N, M)

                    # an anchor that has an IoU overlap higher than 0.7 with any ground-truth box
                    max_iou_each_row = tf.reduce_max(ious, axis=1)
                    rpn_labels = tf.ones(shape=[tf.shape(anchors)[0], ], dtype=tf.float32) * (-1)  # [N, ] # ignored is -1
                    matchs = tf.cast(tf.argmax(ious, axis=1), tf.int32)
                    positives1 = tf.greater_equal(max_iou_each_row, cfgs.RPN_IOU_POSITIVE_THRESHOLD)

                    # the anchor/anchors with the highest Intersection-over-Union (IoU) overlap with a ground-truth box
                    max_iou_each_column = tf.reduce_max(ious, 0)  # (M, )
                    positives2 = tf.reduce_sum(tf.cast(tf.equal(ious, max_iou_each_column), tf.float32), axis=1)

                    positives = tf.logical_or(positives1, tf.cast(positives2, tf.bool))
                    rpn_labels += 2 * tf.cast(positives, tf.float32)

                    anchors_matched_gtboxes = tf.gather(gt_boxes, matchs)  # [N, 4]

                    # background's gtboxes tmp set the first gtbox, it dose not matter, because use object_mask will ignored it
                    negatives = tf.less(max_iou_each_row, cfgs.RPN_IOU_NEGATIVE_THRESHOLD)
                    rpn_labels = rpn_labels + tf.cast(negatives,
                                                      tf.float32)  # [N, ] positive is >=1.0, negative is 0, ignored is -1.0
                    '''
                    Need to note: when positive, labels may >= 1.0.
                    Because, when all the iou< 0.7, we set anchors having max iou each column as positive.
                    these anchors may have iou < 0.3.
                    In the begining, labels is [-1, -1, -1...-1]
                    then anchors having iou<0.3 as well as are max iou each column will be +1.0.
                    when decide negatives, because of iou<0.3, they add 1.0 again.
                    So, the final result will be 2.0
        
                    So, when opsitive, labels may in [1.0, 2.0]. that is labels >=1.0
                    '''
                    positives = tf.cast(tf.greater_equal(rpn_labels, 1.0), tf.float32)
                    ignored = tf.cast(tf.equal(rpn_labels, -1.0), tf.float32) * -1

                    rpn_labels = positives + ignored

                with tf.variable_scope('rpn_minibatch'):
                    # random choose the positive objects
                    positive_indices = tf.reshape(tf.where(tf.equal(rpn_labels, 1.0)),
                                                  [-1])  # use labels is same as object_mask
                    num_of_positives = tf.minimum(tf.shape(positive_indices)[0],
                                                  tf.cast(cfgs.RPN_MINIBATCH_SIZE * cfgs.RPN_POSITIVE_RATE,
                                                          tf.int32))
                    positive_indices = tf.random_shuffle(positive_indices)
                    positive_indices = tf.slice(positive_indices,
                                                begin=[0],
                                                size=[num_of_positives])
                    # random choose the negative objects
                    negatives_indices = tf.reshape(tf.where(tf.equal(rpn_labels, 0.0)), [-1])
                    num_of_negatives = tf.minimum(cfgs.RPN_MINIBATCH_SIZE - num_of_positives,
                                                  tf.shape(negatives_indices)[0])
                    negatives_indices = tf.random_shuffle(negatives_indices)
                    negatives_indices = tf.slice(negatives_indices, begin=[0], size=[num_of_negatives])

                    minibatch_indices = tf.concat([positive_indices, negatives_indices], axis=0)

                    # padding the negative objects if need  但是如果这两项的minibatch_indices大于256的话 应该考虑随机减少一些
                    # 此处默认 正负样本不够256
                    gap = cfgs.RPN_MINIBATCH_SIZE - tf.shape(minibatch_indices)[0]
                    extract_indices = tf.random_shuffle(negatives_indices)
                    extract_indices = tf.slice(extract_indices, begin=[0], size=[gap])
                    minibatch_indices = tf.concat([minibatch_indices, extract_indices], axis=0)  # 再取一些负样本

                    minibatch_indices = tf.random_shuffle(minibatch_indices)
                    # (config.RPN_MINI_BATCH_SIZE, 4)
                    minibatch_anchor_matched_gtboxes = tf.gather(anchors_matched_gtboxes, minibatch_indices)
                    rpn_labels = tf.cast(tf.gather(rpn_labels, minibatch_indices), tf.int32)
                    # encode gtboxes
                    minibatch_anchors = tf.gather(anchors, minibatch_indices)
                    minibatch_encode_gtboxes = encode_and_decode.encode_boxes(unencode_boxes=minibatch_anchor_matched_gtboxes,
                                                                              reference_boxes=minibatch_anchors,
                                                                              scale_factors=cfgs.RPN_BBOX_STD_DEV)
                    rpn_labels_one_hot = tf.one_hot(rpn_labels, 2, axis=-1)

                return minibatch_indices, minibatch_encode_gtboxes, rpn_labels_one_hot


            batch_minibatch_indices, batch_minibatch_encode_gtboxes, batch_rpn_labels_one_hot = \
                boxes_utils.batch_slice([self.gtboxes_and_label],
                                        lambda x:build_slice_rpn_target(x,anchors),batch_size=cfgs.BATCH_SIZE)

            return batch_minibatch_indices, batch_minibatch_encode_gtboxes, batch_rpn_labels_one_hot



    def rpn_losses(self):
        """
        :param minibatch_indices: (batch_size, config.RPN_MINIBATCH_SIZE)
        :param minibatch_encode_gtboxes: (batch_size, config.RPN_MINIBATCH_SIZE, 4)
        :param minibatch_labels_one_hot: (batch_size, config.RPN_MINIBATCH_SIZE, 2)
        :return: the mean of location_loss, classification_loss
        """
        with tf.variable_scope("rpn_losses"):
            batch_minibatch_indices, batch_minibatch_encode_gtboxes, batch_rpn_labels_one_hot = self.build_rpn_train_sample()
            def batch_slice_rpn_target(mini_indices, rpn_encode_boxes, rpn_scores):
                """
                :param mini_indices: (config.RPN_MINIBATCH_SIZE, ) this is indices of anchors
                :param rpn_encode_boxes: (config.RPN_MINIBATCH_SIZE, 4)
                :param rpn_scores: (config.RPN_MINIBATCH_SIZE, 2)
                """
                mini_encode_boxes = tf.gather(rpn_encode_boxes, mini_indices)
                mini_boxes_scores = tf.gather(rpn_scores, mini_indices)

                return mini_encode_boxes, mini_boxes_scores

            mini_encode_boxes, mini_boxes_scores = \
                boxes_utils.batch_slice([batch_minibatch_indices,
                                         self.rpn_encode_boxes,
                                         self.rpn_scores],
                                        lambda x, y, z: batch_slice_rpn_target(x, y, z),
                                        cfgs.BATCH_SIZE)

            object_mask = tf.cast(batch_rpn_labels_one_hot[:, :, 1], tf.float32)
            # losses
            with tf.variable_scope('rpn_location_loss'):
                location_loss = losses.l1_smooth_losses(predict_boxes=mini_encode_boxes,
                                                        gtboxes=batch_minibatch_encode_gtboxes,
                                                        object_weights=object_mask)

            with tf.variable_scope('rpn_classification_loss'):
                classification_loss = tf.losses.softmax_cross_entropy(logits=mini_boxes_scores,
                                                                      onehot_labels=batch_rpn_labels_one_hot)
                classification_loss = tf.cond(tf.is_nan(classification_loss),
                                              lambda: 0.0, lambda: classification_loss)

            return location_loss, classification_loss


    def make_all_anchors(self):
        with tf.variable_scope('make_anchors'):
            anchor_list = []
            level_list = cfgs.LEVEL
            with tf.name_scope('make_anchors_all_level'):
                for level, base_anchor_size, stride in zip(level_list, self.base_anchor_size_list, self.stride):
                    '''
                    (level, base_anchor_size) tuple:
                    (P2, 32), (P3, 64), (P4, 128), (P5, 256), (P6, 512)
                    '''
                    featuremap_height, featuremap_width = tf.shape(self.feature_pyramid[level])[1], \
                                                          tf.shape(self.feature_pyramid[level])[2]

                    tmp_anchors = make_anchors.make_anchors(base_anchor_size, self.anchor_scales, self.anchor_ratios,
                                                           featuremap_height, featuremap_width, stride,
                                                           name='make_anchors_{}'.format(level))
                    tmp_anchors = tf.reshape(tmp_anchors, [-1, 4])
                    anchor_list.append(tmp_anchors)

                all_level_anchors = tf.concat(anchor_list, axis=0)
            return all_level_anchors


    def rpn_proposals(self, is_training):
        """
        param is_training:
        :return:
        rpn_proposals_boxes: (batch_size, config.MAX_PROPOSAL_SIZE, 4)(y1, x1, y2, x2)
        """
        with tf.variable_scope('rpn_proposals'):
            anchors = self.make_all_anchors()
            if is_training:
                rpn_proposals_num = cfgs.MAX_PROPOSAL_NUM_TRAINING
            else:
                rpn_proposals_num = cfgs.MAX_PROPOSAL_NUM_INFERENCE

            def batch_slice_rpn_proposals(rpn_encode_boxes, rpn_scores, anchors, rpn_proposals_num):

                rpn_softmax_scores = slim.softmax(rpn_scores)
                rpn_object_score = rpn_softmax_scores[:, 1]  # second column represent object
                if cfgs.RPN_TOP_K_NMS:
                    top_k_indices = tf.nn.top_k(rpn_object_score, k=cfgs.RPN_TOP_K_NMS).indices
                    rpn_object_score = tf.gather(rpn_object_score, top_k_indices)
                    rpn_encode_boxes = tf.gather(rpn_encode_boxes, top_k_indices)
                    anchors = tf.gather(anchors, top_k_indices)

                rpn_decode_boxes = encode_and_decode.decode_boxes(encode_boxes=rpn_encode_boxes,
                                                                  reference_boxes=anchors,
                                                                  scale_factors=cfgs.RPN_BBOX_STD_DEV)

                valid_indices = tf.image.non_max_suppression(boxes=rpn_decode_boxes,
                                                             scores=rpn_object_score,
                                                             max_output_size=rpn_proposals_num,
                                                             iou_threshold=cfgs.RPN_NMS_IOU_THRESHOLD)
                rpn_decode_boxes = tf.gather(rpn_decode_boxes, valid_indices)
                rpn_object_score = tf.gather(rpn_object_score, valid_indices)
                # clip proposals to img boundaries(replace the out boundary with image boundary)
                rpn_decode_boxes = boxes_utils.clip_boxes_to_img_boundaries(rpn_decode_boxes, self.image_height,self.image_width)
                # Pad if needed
                # 依然默认筛选后的decode_boxes是不够 最大值的 2000
                padding = tf.maximum(rpn_proposals_num - tf.shape(rpn_decode_boxes)[0], 0)
                # care about why we don't use tf.pad in there
                zeros_padding = tf.zeros((padding, 4), dtype=tf.float32)
                rpn_proposals_boxes = tf.concat([rpn_decode_boxes, zeros_padding], axis=0)
                rpn_object_score = tf.pad(rpn_object_score, [(0, padding)])

                return rpn_proposals_boxes, rpn_object_score

            rpn_proposals_boxes, rpn_object_scores = \
                boxes_utils.batch_slice([self.rpn_encode_boxes, self.rpn_scores],
                                        lambda x, y: batch_slice_rpn_proposals(x, y, anchors, rpn_proposals_num),
                                        cfgs.BATCH_SIZE)

            return rpn_proposals_boxes, rpn_object_scores




