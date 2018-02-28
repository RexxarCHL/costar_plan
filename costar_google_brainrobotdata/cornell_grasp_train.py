#!/usr/local/bin/python
'''
Training a network on cornell grasping dataset for detecting grasping positions.

Apache License 2.0 https://www.apache.org/licenses/LICENSE-2.0

Cornell Dataset Code Based on:
    https://github.com/tnikolla/robot-grasp-detection

'''
import os
import re
import errno
import sys
import json
import csv
import argparse
import os.path
import glob
import datetime
import tensorflow as tf
import numpy as np
import six
from shapely.geometry import Polygon
import cornell_grasp_dataset_reader
import time
from tensorflow.python.platform import flags

# progress bars https://github.com/tqdm/tqdm
# import tqdm without enforcing it as a dependency
try:
    from tqdm import tqdm
except ImportError:

    def tqdm(*args, **kwargs):
        if args:
            return args[0]
        return kwargs.get('iterable', None)


from keras import backend as K
import keras
import keras_contrib
import keras_tqdm
from keras.layers import Input, Dense, Concatenate
from keras.callbacks import ReduceLROnPlateau
from keras.callbacks import CSVLogger
from keras.callbacks import EarlyStopping
from keras.callbacks import ModelCheckpoint
from keras.callbacks import TensorBoard
from keras.models import Model
from keras.models import model_from_json
from grasp_model import concat_images_with_tiled_vector_layer
from grasp_model import top_block
from grasp_model import create_tree_roots
from grasp_model import choose_hypertree_model
from cornell_grasp_dataset_reader import parse_and_preprocess

from callbacks import EvaluateInputGenerator
from callbacks import PrintLogsCallback
from callbacks import FineTuningCallback

import grasp_loss
import grasp_metrics
import grasp_utilities


flags.DEFINE_float(
    'learning_rate',
    0.02,
    'Initial learning rate.'
)
flags.DEFINE_float(
    'fine_tuning_learning_rate',
    0.001,
    'Initial learning rate, this is the learning rate used if load_weights is passed.'
)
flags.DEFINE_integer(
    'fine_tuning_epochs',
    100,
    'Number of epochs to run trainer with all weights marked as trainable.'
)
flags.DEFINE_integer(
    'epochs',
    200,
    'Number of epochs to run trainer.'
)
flags.DEFINE_integer(
    'batch_size',
    16,
    'Batch size.'
)
flags.DEFINE_string(
    'log_dir',
    './logs_cornell/',
    'Directory for tensorboard, model layout, model weight, csv, and hyperparam files'
)
flags.DEFINE_string(
    'model_path',
    '/tmp/tf/model.ckpt',
    'Variables for the model.'
)
flags.DEFINE_string(
    'train_or_validation',
    'validation',
    'deprecated, does nothing. Train or evaluate the dataset'
)
flags.DEFINE_string(
    'run_name',
    '',
    'A string that will become part of the logged directories and filenames.'
)
flags.DEFINE_integer(
    'num_folds',
    5,
    'Total number of folds, how many times should the data be split between training and validation'
)
flags.DEFINE_integer(
    'num_train',
    8,
    'Number of files used for training in one fold, '
    'must be less than the number of tfrecord files, aka splits.'
)
flags.DEFINE_integer(
    'num_validation',
    2,
    'Number of tfrecord files for validation.'
    'must be less than the number of tfrecord files, aka splits.'
    'This number also automatically determines the number of folds '
    'when running when the pipeline_stage flag includes k_fold.'
)
flags.DEFINE_integer(
    'num_test',
    0,
    'num of fold used for the test dataset'
    'must be less than the number of tfrecord files, aka splits.'
    'This must be 0 when the pipeline_stage flag includes k_fold'
)
flags.DEFINE_string('load_weights', None,
                    """Path to hdf5 file containing model weights to load and continue training.""")
flags.DEFINE_string('load_hyperparams', None,
                    """Load hyperparams from a json file.""")
flags.DEFINE_string('pipeline_stage', 'train_test',
                    """Choose to "train", "test", "train_test", or "train_test_kfold" with the grasp_dataset
                       data for training and grasp_dataset_test for testing.""")
flags.DEFINE_string(
    'split_dataset', 'objectwise',
    """Options are imagewise and objectwise, this is the type of split chosen when the tfrecords were generated.""")
flags.DEFINE_string('tfrecord_filename_base', 'cornell-grasping-dataset', 'base of the filename used for the dataset tfrecords and csv files')
flags.DEFINE_string(
    'feature_combo', 'image_preprocessed_sin2_cos2_width_3',
    """
    feature_combo: The name for the combination of input features being utilized.
        Options include 'image_preprocessed', image_preprocessed_width_1,
        'image_preprocessed_sin2_cos2_width_3'
        See choose_features_and_metrics() for details.
    """
)
flags.DEFINE_string(
    'problem_type', 'grasp_classification',
    """Choose between different formulations of the grasping task.
    Problem type options are 'segmentation', 'classification',
    'image_center_grasp_regression',
    'grasp_regression' which tries to predict successful grasp bounding boxes,
    'grasp_classification'
    'grasp_segmentation' which tries to classify input grasp parameters at each pixel.
    'pixelwise_grasp_regression' which tries to predict successful grasp bounding boxes at each pixel.

    """
)
flags.DEFINE_boolean(
    'fine_tuning', True,
    """ If true the model will be fine tuned.

        This means that any imagenet weights will be made trainable,
        and the learning rate will be set to fine_tuning_learning_rate.
    """)
flags.DEFINE_string(
    'kfold_params', None,
    """ Load the json file containing parameters from a kfold cross validation run.
    """
)

FLAGS = flags.FLAGS

# TODO(ahundt) put these utility functions in utils somewhere


def run_training(
        learning_rate=None,
        batch_size=None,
        num_gpus=1,
        top='classification',
        epochs=None,
        preprocessing_mode=None,
        train_data=None,
        validation_data=None,
        train_filenames=None,
        train_size=None,
        val_filenames=None,
        val_size=None,
        test_filenames=None,
        test_size=None,
        feature_combo_name=None,
        problem_name=None,
        image_model_name='vgg',
        optimizer_name='sgd',
        log_dir=None,
        hyperparams=None,
        load_weights=None,
        pipeline=None,
        run_name=None,
        fine_tuning_learning_rate=None,
        fine_tuning=None,
        fine_tuning_epochs=None,
        **kwargs):
    """

    top: options are 'segmentation' and 'classification'.
    problem_name: options are 'grasp_regression', 'grasp_classification',
        'pixelwise_grasp_regression', 'pixelwise_grasp_classification',
        'image_center_grasp_regression'. Image center grasp regression is
        a pretraining step for pixel
        Make sure this is properly coordinated with 'top' param.
    feature_combo_name: The name for the combination of input features being utilized.
        Options include 'image_preprocessed', image_preprocessed_width_1,
        image_preprocessed_sin2_cos2_width_3
        'grasp_regression', image_center_grasp_regression.
        See choose_features_and_metrics() for details.
    hyperparams: a dictionary of hyperparameter selections made for this training run.
       If provided these values will simply be dumped to a file and
       not utilized in any other way.
    """
    if epochs is None:
        epochs = FLAGS.epochs
    if batch_size is None:
        batch_size = FLAGS.batch_size
    if learning_rate is None:
        learning_rate = FLAGS.learning_rate
    if log_dir is None:
        log_dir = FLAGS.log_dir
    if load_weights is None:
        load_weights = FLAGS.load_weights
    if pipeline is None:
        pipeline = FLAGS.pipeline_stage
    if problem_name is None:
        problem_name = FLAGS.problem_type
    if run_name is None:
        run_name = FLAGS.run_name
    if fine_tuning_learning_rate is None:
        fine_tuning_learning_rate = FLAGS.fine_tuning_learning_rate
    if fine_tuning is None:
        fine_tuning = FLAGS.fine_tuning
    if fine_tuning_epochs is None:
        fine_tuning_epochs = FLAGS.fine_tuning_epochs
    if feature_combo_name is None:
        feature_combo_name = FLAGS.feature_combo

    [image_shapes, vector_shapes, data_features, model_name,
     monitor_loss_name, label_features, monitor_metric_name,
     loss, metrics, classes, success_only] = choose_features_and_metrics(feature_combo_name, problem_name)

    # see parse_and_preprocess() for the creation of these features
    model_name = image_model_name + model_name

    # If loading pretrained weights
    # it is very important to preprocess
    # in exactly the same way the model
    # was originally trained
    preprocessing_mode = choose_preprocessing_mode(preprocessing_mode, image_model_name)

    # choose hypertree_model with inputs [image], [sin_theta, cos_theta]
    model = choose_hypertree_model(
        image_shapes=image_shapes,
        vector_shapes=vector_shapes,
        top=top,
        classes=classes,
        image_model_name=image_model_name,
        **kwargs)

    if load_weights:
        if os.path.isfile(load_weights):
            model.load_weights(load_weights)
        else:
            print('Could not load weights {}, '
                  'the file does not exist, '
                  'starting fresh....'.format(load_weights))
    print('Saving weights as ' + monitor_loss_name + ' improves.')
    # TODO(ahundt) add a loss that changes size with how open the gripper is
    # loss = grasp_loss.segmentation_gaussian_measurement

    dataset_names_str = 'cornell_grasping'
    run_name = grasp_utilities.timeStamped(run_name + '-' + model_name + '-dataset_' + dataset_names_str + '-' + label_features[0])
    callbacks = []

    callbacks, optimizer = choose_optimizer(optimizer_name, learning_rate, callbacks, monitor_loss_name)

    log_dir = os.path.join(log_dir, run_name)
    print('Writing logs for models, accuracy and tensorboard in ' + log_dir)
    log_dir_run_name = os.path.join(log_dir, run_name)
    csv_logger = CSVLogger(log_dir_run_name + '.csv')
    callbacks = callbacks + [csv_logger]
    callbacks += [PrintLogsCallback()]
    grasp_utilities.mkdir_p(log_dir)

    # Save the hyperparams to a json string so it is human readable
    if hyperparams is not None:
        with open(log_dir_run_name + '_hyperparams.json', 'w') as fp:
            json.dump(hyperparams, fp)

    # Save the current model to a json string so it is human readable
    with open(log_dir_run_name + '_model.json', 'w') as fp:
        fp.write(model.to_json())

    checkpoint = keras.callbacks.ModelCheckpoint(
        log_dir_run_name + '-epoch-{epoch:03d}-' +
        monitor_loss_name + '-{' + monitor_loss_name + ':.3f}-' +
        monitor_metric_name + '-{' + monitor_metric_name + ':.3f}.h5',
        save_best_only=True, verbose=1, monitor=monitor_metric_name)

    callbacks = callbacks + [checkpoint]
    # An additional useful param is write_batch_performance:
    #  https://github.com/keras-team/keras/pull/7617
    #  write_batch_performance=True)
    progress_tracker = TensorBoard(log_dir=log_dir, write_graph=True,
                                   write_grads=False, write_images=False,
                                   histogram_freq=0, batch_size=batch_size)
                                   # histogram_freq=0, batch_size=batch_size,
                                   # write_batch_performance=True)
    callbacks = callbacks + [progress_tracker]

    # make sure the TQDM callback is always the final one
    callbacks += [keras_tqdm.TQDMCallback()]

    #TODO(ahundt) enable when https://github.com/keras-team/keras/pull/9105 is resolved
    # callbacks += [FineTuningCallback(epoch=0)]

    if num_gpus > 1:
        parallel_model = keras.utils.multi_gpu_model(model, num_gpus)
    else:
        parallel_model = model

    parallel_model.compile(
        optimizer=optimizer,
        loss=loss,
        metrics=metrics)

    train_data, train_steps, validation_data, validation_steps, test_data, test_steps = load_dataset(
        train_filenames=train_filenames, train_size=train_size,
        val_filenames=val_filenames, val_size=val_size,
        test_filenames=test_filenames, test_size=test_size,
        label_features=label_features, data_features=data_features, batch_size=batch_size,
        train_data=train_data, validation_data=validation_data, preprocessing_mode=preprocessing_mode,
        success_only=success_only
    )

    # Get the validation dataset in one big numpy array for validation
    # This lets us take advantage of tensorboard visualization
    if 'train' in pipeline:
        if 'test' in pipeline:
            # we need this callback to be at the beginning!
            callbacks = [EvaluateInputGenerator(generator=test_data,
                                                steps=test_steps,
                                                metrics_prefix='test',
                                                verbose=0)] + callbacks

        # print('calling model.fit_generator()')
        history = parallel_model.fit_generator(
            train_data,
            steps_per_epoch=train_steps,
            epochs=epochs,
            validation_data=validation_data,
            validation_steps=validation_steps,
            callbacks=callbacks,
            verbose=0)

        #  TODO(ahundt) remove when FineTuningCallback https://github.com/keras-team/keras/pull/9105 is resolved
        if fine_tuning:
            # do fine tuning stage after initial training
            print('')
            print('')
            print('Initial training complete, beginning fine tuning stage')
            print('------------------------------------------------------')
            _, optimizer = choose_optimizer(optimizer_name, fine_tuning_learning_rate, [], monitor_loss_name)

            for layer in parallel_model.layers:
                layer.trainable = True

            parallel_model.compile(
                optimizer=optimizer,
                loss=loss,
                metrics=metrics)

            history = parallel_model.fit_generator(
                train_data,
                steps_per_epoch=train_steps,
                epochs=epochs + fine_tuning_epochs,
                validation_data=validation_data,
                validation_steps=validation_steps,
                callbacks=callbacks,
                verbose=0,
                initial_epoch=epochs)

    elif 'test' in pipeline:
        history = parallel_model.evaluate_generator(generator=test_data, steps=test_steps)
    else:
        raise ValueError('unknown pipeline configuration ' + pipeline + ' chosen, try '
                         'train, test, train_test, or train_test_kfold')

    model.save_weights(log_dir_run_name + '_model_weights.h5')

    print('')
    print('')
    print('This training run is complete')
    print('------------------------------------------------------')
    return history


def get_compiled_model(learning_rate=None,
                       batch_size=None,
                       num_gpus=1,
                       top='classification',
                       epochs=None,
                       preprocessing_mode=None,
                       input_filenames=None,
                       feature_combo_name='image_preprocessed_sin2_cos2_width_3',
                       problem_name=None,
                       image_model_name='vgg',
                       optimizer_name='sgd',
                       log_dir=None,
                       hyperparams=None,
                       load_weights=None,
                       pipeline=None,
                       train_size=None,
                       val_size=None,
                       test_size=None,
                       run_name=None,
                       train_filenames=None,
                       test_filenames=None,
                       val_filenames=None,
                       **kwargs):
    """
    Get a compiled model instance and input data.
    input_filenames: path to tfrecord want to load.

    top: options are 'segmentation' and 'classification'.
    problem_name: options are 'grasp_regression', 'grasp_classification',
        'pixelwise_grasp_regression', 'pixelwise_grasp_classification',
        'image_center_grasp_regression'. Image center grasp regression is
        a pretraining step for pixel
        Make sure this is properly coordinated with 'top' param.
    feature_combo_name: The name for the combination of input features being utilized.
        Options include 'image_preprocessed', image_preprocessed_width_1,
        image_preprocessed_sin2_cos2_width_3
        'grasp_regression', image_center_grasp_regression.
        See choose_features_and_metrics() for details.
    hyperparams: a dictionary of hyperparameter selections made for this training run.
       If provided these values will simply be dumped to a file and
       not utilized in any other way.
    """
    if epochs is None:
        epochs = FLAGS.epochs
    if batch_size is None:
        batch_size = FLAGS.batch_size
    if log_dir is None:
        log_dir = FLAGS.log_dir
    if learning_rate is None:
        if load_weights is None:
            learning_rate = FLAGS.learning_rate
        else:
            learning_rate = FLAGS.fine_tuning_learning_rate
    if load_weights is None:
        load_weights = FLAGS.load_weights
    if problem_name is None:
        problem_name = FLAGS.problem_type

    [image_shapes, vector_shapes, data_features, model_name,
     monitor_loss_name, label_features, monitor_metric_name,
     loss, metrics, classes, success_only] = choose_features_and_metrics(feature_combo_name, problem_name)

    # see parse_and_preprocess() for the creation of these features
    model_name = image_model_name + model_name

    # If loading pretrained weights
    # it is very important to preprocess
    # in exactly the same way the model
    # was originally trained
    preprocessing_mode = choose_preprocessing_mode(preprocessing_mode, image_model_name)

    # choose hypertree_model with inputs [image], [sin_theta, cos_theta]
    model = choose_hypertree_model(
        image_shapes=image_shapes,
        vector_shapes=vector_shapes,
        top=top,
        classes=classes,
        image_model_name=image_model_name,
        **kwargs)

    # if num_gpus > 1:
    #     parallel_model = keras.utils.multi_gpu_model(model, num_gpus)
    # else:
    #     parallel_model = model

    model.load_weights(load_weights)

    model.compile(
        optimizer=optimizer_name,
        loss=loss,
        metrics=metrics)

    return model


def choose_preprocessing_mode(preprocessing_mode, image_model_name):
    """ Choose preprocessing for specific pretrained weights
    it is very important to preprocess
    in exactly the same way the model
    was originally trained
    """
    if preprocessing_mode is None:
        if 'densenet' in image_model_name:
            preprocessing_mode = 'torch'
        elif 'nasnet' in image_model_name:
            preprocessing_mode = 'tf'
        elif 'vgg' in image_model_name:
            preprocessing_mode = 'tf'
        elif 'resnet' in image_model_name:
            preprocessing_mode = 'caffe'
        else:
            raise ValueError('You need to explicitly set the preprocessing mode to '
                             'torch, tf, or caffe for these weights')
    return preprocessing_mode


def choose_optimizer(optimizer_name, learning_rate, callbacks, monitor_loss_name):
    if optimizer_name == 'sgd':
        optimizer = keras.optimizers.SGD(learning_rate * 1.0)
        callbacks = callbacks + [
            # Reduce the learning rate if training plateaus.
            keras.callbacks.ReduceLROnPlateau(patience=12, verbose=1, factor=0.5, monitor=monitor_loss_name)
        ]
    elif optimizer_name == 'adam':
        optimizer = keras.optimizers.Adam()
    else:
        raise ValueError('Unsupported optimizer ' + str(optimizer_name) +
                         'try adam or sgd.')
    return callbacks, optimizer


def train_k_fold(split_type=None,
                 tfrecord_filename_base=None, csv_path='-k-fold-stat.csv',
                 log_dir=None, run_name=None, num_validation=None,
                 num_train=None, num_test=None, **kwargs):
    """ Do K_Fold training

    Please be aware that the number of train and validation
    steps changes every time the dataset is converted.
    These values are automatically loaded from a csv file,
    but be certain you do not mix the csv files up or
    overwrite the datasets and csv files separately.


    # Important Note

    Do not change this function without special care because we have
    files saved from past k-fold runs that use specific strings which
    have been written out to json files, and thus will remain the way they are... forever.

    # Arguments

        split_type: str, either 'imagewise' or 'objectwise', should be consistent with
        splits type desired when doing actual splits.
    """
    if log_dir is None:
        log_dir = FLAGS.log_dir
    if run_name is None:
        run_name = FLAGS.run_name
    if split_type is None:
        split_type = FLAGS.split_dataset
    if tfrecord_filename_base is None:
        tfrecord_filename_base = FLAGS.tfrecord_filename_base
    if num_validation is None:
        num_validation = FLAGS.num_validation
    if num_test is None:
        num_test = FLAGS.num_test
    if num_train is None:
        num_train = FLAGS.num_train

    if num_test != 0:
        raise ValueError('k_fold training does not support test data. '
                         'Check the command line flags and set --num_test 0')
    cur_csv_path = os.path.join(FLAGS.data_dir, tfrecord_filename_base + '-' + split_type + csv_path)
    csv_reader = csv.DictReader(open(cur_csv_path, mode='r'))
    unique_image_num = []
    num_splits = None
    for row in csv_reader:
        if num_splits is None:
            num_splits = int(row[' num_splits'])
        unique_image_num.append(int(row[' num_total_grasp']))  # file writer repeat each image for num_grasp

    num_fold, divides_evenly = np.divmod(num_splits, num_validation)
    # If this fails you need to fix the number of folds splits the dataset splits evenly!
    if divides_evenly != 0:
        raise ValueError('You must ensure the num_validation flag divides evenly '
                         'with the number of tfrecord files, aka splits. '
                         'Currently %s files divided by --num_validation %s results'
                         ' in %s folds and but this '
                         'leaves a remainder of %s.' % (str(num_validation),
                                                        str(num_fold),
                                                        str(num_splits),
                                                        str(divides_evenly)))

    val_filenames = []
    train_filenames = []
    train_id = ''
    val_size = 0
    train_size = 0
    kfold_run_name = grasp_utilities.timeStamped(run_name + '-' + split_type + '-kfold')
    log_dir = os.path.join(log_dir, kfold_run_name)
    kfold_param_dicts = {'num_fold': num_fold, 'num_splits': num_splits, 'fold_size': num_train}
    kfold_run_train_param_list = []
    fold_name_list = []
    # create the directory we will log to
    grasp_utilities.mkdir_p(log_dir)

    # 2k files, but k folds, so read two file at a time
    for i in tqdm(range(num_fold), desc='Preparing kfold'):
        # This is a special string,
        # make sure to maintain backwards compatibility
        # if you modify it.
        fold_name = 'fold-' + str(i)
        fold_name_list += [fold_name]

        val_filenames = []
        val_sizes = []
        val_id = ''
        for k in range(num_validation):
            current_file_index = num_validation * i + k
            val_id += str(current_file_index)
            val_filenames += [os.path.join(FLAGS.data_dir, tfrecord_filename_base + '-' + split_type + '-fold-' + str(current_file_index) + '.tfrecord')]
            val_sizes += [unique_image_num[current_file_index]]
        val_size = sum(val_sizes)

        train_filenames = []
        train_sizes = []
        train_id = ''
        for j in range(num_fold):
            if j == i:
                continue
            for k in range(num_validation):
                current_file_index = num_validation * j + k
                train_id += str(current_file_index)
                train_filenames += [os.path.join(FLAGS.data_dir, tfrecord_filename_base + '-' + split_type + '-fold-' + str(current_file_index) + '.tfrecord')]
                train_sizes += [unique_image_num[current_file_index]]
        train_size = sum(train_sizes)

        save_splits_weights = run_name + '-' + fold_name + '-' + split_type + '-train-on-' + train_id + '-val-on-' + val_id
        print('Preparing fold ' + str(i) + ' train dataset splits: ' + train_id + ',   val dataset splits: ' + val_id)
        training_run_params = dict(
            train_filenames=train_filenames, val_filenames=val_filenames, pipeline='train_val',
            train_size=train_size, val_size=val_size,
            log_dir=log_dir, run_name=save_splits_weights,
            **kwargs)
        kfold_param_dicts[fold_name + '-val-ids'] = val_id
        kfold_param_dicts[fold_name + '-train-ids'] = train_id
        kfold_param_dicts[fold_name] = training_run_params
        kfold_run_train_param_list += [training_run_params]
        train_id = ''
        train_size = 0

    # save all folds to disk so we can recover exactly what happened
    json_params_path = os.path.join(log_dir, kfold_run_name + '_params.json')
    with open(json_params_path, 'w') as fp:
        # save out all kfold params so they can be reloaded in the future
        json.dump(kfold_param_dicts, fp)

    # json_histories_path = os.path.join(log_dir, kfold_run_name + '_histories.json')
    run_histories = {}
    progbar_fold_name_list = tqdm(fold_name_list, desc='Training k_fold')
    for i, (params, fold_name) in enumerate(zip(kfold_run_train_param_list, progbar_fold_name_list)):
        progbar_fold_name_list.write('\n------------------------------------------\n'
                                     'Training fold ' + str(i) + ' of ' + str(len(fold_name_list)) + '\n'
                                     '\n------------------------------------------\n')
        history = run_training(**params)
        run_histories[fold_name] = history
        progbar_fold_name_list.update()
        # TODO(ahundt) save histories in some nice way
        # with open(json_histories_path, 'w') as fp:
        #     # save out all kfold params so they can be reloaded in the future
        #     json.dump(run_histories, fp)

    return run_histories


def choose_features_and_metrics(feature_combo_name, problem_name, image_shapes=None):
    """ Choose the features to load from the dataset and losses to use during training
    """
    if image_shapes is None:
        image_shapes = [(FLAGS.resize_height, FLAGS.resize_width, 3)]

    # most cases are 0 to 1 outputs
    classes = 1
    # most cases consider both grasp successes and grasp failures
    success_only = False

    # Configure the input data dimensions
    # TODO(ahundt) get input dimensions automatically, based on configured params
    if feature_combo_name == 'image/preprocessed' or feature_combo_name == 'image_preprocessed':
        data_features = ['image/preprocessed']
        vector_shapes = None
    elif feature_combo_name == 'image_preprocessed_sin2_cos2_height_width_4':
        data_features = ['image/preprocessed', 'preprocessed_sin2_cos2_height_width_4']
        vector_shapes = [(4,)]
    elif feature_combo_name == 'image_preprocessed_sin_cos_width_3':
        data_features = ['image/preprocessed', 'preprocessed_sin_cos_width_3']
        vector_shapes = [(3,)]
    elif feature_combo_name == 'image_preprocessed_sin2_cos2_width_3':
        data_features = ['image/preprocessed', 'preprocessed_sin2_cos2_width_3']
        vector_shapes = [(3,)]
    elif feature_combo_name == 'image_preprocessed_width_1':
        data_features = ['image/preprocessed', 'bbox/width']
        vector_shapes = [(1,)]
    elif feature_combo_name == 'preprocessed':
        data_features = ['image/preprocessed', 'bbox/preprocessed/cy_cx_normalized_2',
                         'bbox/preprocessed/sin_cos_2', 'bbox/preprocessed/logarithm_height_width_2']
        vector_shapes = [(2,), (2,), (2,)]
    elif feature_combo_name == 'raw':
        data_features = ['image/decoded', 'sin_cos_height_width_4']
        vector_shapes = [(4,)]
    else:
        raise ValueError('Selected feature ' + str(feature_combo_name) + ' does not exist. '
                         'feature selection options are image_preprocessed_sin_cos_height_width_4, '
                         'image_preprocessed_sin_cos_height_3, image_preprocessed_height_1,'
                         'preprocessed, and raw')

    # Configure the chosen problem type such as
    # classifying proposed grasps,
    # predicting a single grasp, or
    # predicting a grasp at each pixel.
    if problem_name == 'segmentation' or problem_name == 'grasp_segmentation' or problem_name == 'pixelwise_grasp_classification':
        label_features = ['grasp_success_yx_3']
        monitor_loss_name = 'segmentation_gaussian_binary_crossentropy'
        monitor_metric_name = 'val_segmentation_single_pixel_binary_accuracy'
        loss = grasp_loss.segmentation_gaussian_binary_crossentropy
        metrics = [grasp_loss.segmentation_single_pixel_binary_accuracy, grasp_loss.mean_pred]
        model_name = '_dilated_model'
    elif problem_name == 'classification' or problem_name == 'grasp_classification':
        label_features = ['grasp_success']
        monitor_metric_name = 'val_binary_accuracy'
        monitor_loss_name = 'val_loss'
        metrics = ['binary_accuracy', grasp_loss.mean_pred, grasp_loss.mean_true]
        loss = keras.losses.binary_crossentropy
        model_name = '_dense_model'
    elif problem_name == 'grasp_regression':
        # predicting a single grasp proposal
        success_only = True
        label_features = ['norm_sin2_cos2_hw_yx_6']
        monitor_metric_name = 'val_grasp_jaccard'
        monitor_loss_name = 'val_loss'
        metrics = [grasp_metrics.grasp_jaccard, keras.losses.mean_squared_error, grasp_loss.mean_pred, grasp_loss.mean_true]
        loss = keras.losses.mean_squared_error
        model_name = '_regression_model'
        classes = 6
    elif problem_name == 'image_center_grasp_regression':
        # predicting a single grasp proposal at the image center
        label_features = ['grasp_success_norm_sin2_cos2_hw_5']
        monitor_metric_name = 'grasp_jaccard'
        monitor_loss_name = 'val_loss'
        metrics = [grasp_metrics.grasp_jaccard, keras.losses.mean_squared_error, grasp_loss.mean_pred, grasp_loss.mean_true]
        loss = keras.losses.mean_squared_error
        model_name = '_center_regression_model'
        classes = 5
    elif problem_name == 'pixelwise_grasp_regression':
        raise NotImplementedError
    else:
        raise ValueError('Selected problem_name ' + str(problem_name) + ' does not exist. '
                         'feature selection options are segmentation and classification, '
                         'image_center_grasp_regression, grasp_regression, grasp_classification'
                         'grasp_segmentation')

    if('classification' in problem_name and 'height' in feature_combo_name):
        print(
            """
            # WARNING: DO NOT use height as an input for classification tasks,
            # except to demonstrate the problems described below!
            #
            # The "height" parameter indicates the length of the graspable region,
            # which is highly correlated with the ground truth grasp_success
            # and would not be useful on a real robot.
            # Note that these parameters are used for classification
            # results in several previous papers but they also include
            # regression results and are thus OK overall.
            #
            # Instead, we suggest using image_preprocessed_sin2_cos2_width_3,
            # width is a proposed gripper openness which should be OK.
            """)

    return image_shapes, vector_shapes, data_features, model_name, monitor_loss_name, label_features, monitor_metric_name, loss, metrics, classes, success_only


def load_dataset(
        label_features=None, data_features=None,
        train_filenames=None, train_size=0,
        val_filenames=None, val_size=0,
        test_filenames=None, test_size=0,
        batch_size=None,
        val_batch_size=1, test_batch_size=1,
        train_data=None, validation_data=None, test_data=None,
        in_memory_validation=False,
        preprocessing_mode='tf', success_only=False):
    """ Load the cornell grasping dataset from the file if it isn't already available.

    # Arguments

    success_only: only traverse successful grasps
    preprocessing_mode: 'tf', 'caffe', or 'torch' preprocessing.
        See keras/applications/imagenet_utils.py for details.

    """
    if train_filenames is None and val_filenames is None and test_filenames is None:
        # train/val/test filenames are generated from the CSV file if they aren't provided
        train_filenames, train_size, val_filenames, val_size, test_filenames, test_size = epoch_params()
    train_steps, val_steps, test_steps = epoch_params_for_splits(
        train_batch=batch_size, val_batch=val_batch_size, test_batch=test_batch_size,
        samples_train=train_size, samples_val=val_size, samples_test=test_size)

    if in_memory_validation:
        val_batch_size = val_size

    if validation_data is None and val_filenames is not None:
        validation_data = cornell_grasp_dataset_reader.yield_record(
            val_filenames, label_features, data_features,
            batch_size=val_batch_size,
            parse_example_proto_fn=parse_and_preprocess,
            preprocessing_mode=preprocessing_mode,
            apply_filter=success_only,
            is_training=False)

    if in_memory_validation:
        print('loading validation data directly into memory, if you run out set in_memory_validation to False')
        validation_data = next(validation_data)

    if train_data is None and train_filenames is not None:
        train_data = cornell_grasp_dataset_reader.yield_record(
            train_filenames, label_features, data_features,
            batch_size=batch_size,
            parse_example_proto_fn=parse_and_preprocess,
            preprocessing_mode=preprocessing_mode,
            apply_filter=success_only,
            is_training=True)

    if test_data is None and test_filenames is not None:
        test_data = cornell_grasp_dataset_reader.yield_record(
            test_filenames, label_features, data_features,
            batch_size=test_batch_size,
            parse_example_proto_fn=parse_and_preprocess,
            preprocessing_mode=preprocessing_mode,
            apply_filter=success_only,
            is_training=False)

    return train_data, train_steps, validation_data, val_steps, test_data, test_steps


def model_predict_k_fold(kfold_params=None, verbose=0):
    """ Load past runs and make predictions with the model and data.
        model: compiled model instance.
        input_data: generator instance.
        kfold_params: a path to a json file containing parameters from a previous k_fold cross validation run



    # Important Note

    Do not change this function without special care because we have
    files saved from past k-fold runs that use specific strings which
    have been written out to json files, and thus will remain the way they are... forever.

    """

    print(""" Predicting on kfold results.
    Double check that you've actually got the best model checkpoint.
    We currently take the last checkpoint file in the folder
    using whatever ordering your OS provides.
    """)
    kfold_params_dir = None

    if kfold_params is not None and isinstance(kfold_params, str):
        kfold_params_dir = os.path.dirname(kfold_params)
        with open(kfold_params, mode='r') as kfold_json_file:
            kfold_param_dicts = json.load(kfold_json_file)
    else:
        raise ValueError('kfold_params can only be a path to a json file generated by train_k_fold() at this time.')

    num_fold = kfold_param_dicts['num_fold']

    fold_averages = np.zeros((num_fold))
    with tqdm(range(num_fold), desc='kfold prediction') as progbar_folds:
        for i in progbar_folds:
            # This is a special string,
            # make sure to maintain backwards compatibility
            # if you modify it.
            fold_name = 'fold-' + str(i)

            # load all the settings from a past run
            training_run_params = kfold_param_dicts[fold_name]

            val_filenames = training_run_params['val_filenames']
            log_dir = training_run_params['log_dir']
            # we prefix every fold with a timestamp, therefore we assume:
            #   lexicographic order == time order == fold order
            # '200_epoch_real_run' is for backwards compatibility before
            # the fold nums were put into each fold's log_dir and run_name.
            directory_listing = os.listdir(log_dir)
            fold_log_dir = []
            for name in directory_listing:
                name = os.path.join(log_dir, name)
                if os.path.isdir(name):
                    if '200_epoch_real_run' in name or fold_name in name:
                        fold_log_dir += [name]

            if len(fold_log_dir) > 1:
                # more backwards compatibility tricks
                fold_log_dir = fold_log_dir[i]
            else:
                # this should work in most cases excluding the first k_fold run log
                [fold_log_dir] = fold_log_dir

            # Now we have to load the best model
            # '200_epoch_real_run' is for backwards compatibility before
            # the fold nums were put into each fold's log_dir and run_name.
            directory_listing = os.listdir(fold_log_dir)
            fold_checkpoint_files = []
            for name in directory_listing:
                name = os.path.join(fold_log_dir, name)
                if not os.path.isdir(name) and '.h5' in name:
                    if '200_epoch_real_run' in name or fold_name in name:
                        fold_checkpoint_files += [name]

            # check the filenames for the highest val score
            fold_checkpoint_file = None
            best_val = 0.0
            for filename in fold_checkpoint_files:
                if 'val_' in filename:
                    # pull out all the floating point numbers
                    # source: https://stackoverflow.com/a/4703409/99379
                    nums = re.findall(r"[-+]?\d*\.\d+|\d+", filename)
                    if len(nums) > 0:
                        # don't forget about the .h5 at the end...
                        cur_num = np.abs(float(nums[-2]))
                        if verbose > 0:
                            progbar_folds.write('old best ' + str(best_val) + ' current ' + str(cur_num))
                        if cur_num > best_val:
                            if verbose > 0:
                                progbar_folds.write('new best: ' + str(cur_num) + ' file: ' + filename)
                            best_val = cur_num
                            fold_checkpoint_file = filename

            if fold_checkpoint_file is None:
                raise ValueError('\n\nSomething went wrong when looking for model checkpoints, '
                                 'you need to take a look at model_predict_k_fold() '
                                 'in cornell_grasp_train.py. Here are the '
                                 'model checkpoint files we were looking at: \n\n' +
                                 str(fold_checkpoint_files))

            progbar_folds.write('Fold ' + str(i) + ' Loading checkpoint: ' + str(fold_checkpoint_file))

            # load the model
            model = get_compiled_model(load_weights=fold_checkpoint_file, **training_run_params)

            model.load_weights(fold_checkpoint_file)

            # TODO(ahundt) low priority: automatically choose feature and metric strings
            # choose_features_and_metrics(feature_combo_name, problem_name)
            prediction_name = 'norm_sin2_cos2_hw_yx_6'
            metric_name = 'val_grasp_jaccard'
            unique_score_category = 'image/filename'
            data_features = ['image/preprocessed']
            metric_fn = grasp_metrics.jaccard_score

            # TODO(ahundt) low-medium priority: save iou scores
            # metric_name = 'intersection_over_union'

            # Load the validation data and traverse it exactly once
            input_data = cornell_grasp_dataset_reader.yield_record(
                val_filenames, batch_size=1, is_training=False,
                shuffle=False, steps=1,
                preprocessing_mode=training_run_params['preprocessing_mode'])

            # go over every data entry
            best_results_for_each_image = {}
            try:
                for example_dict in tqdm(input_data):
                    sess = K.get_session()
                    init_g = tf.global_variables_initializer()
                    init_l = tf.local_variables_initializer()
                    sess.run(init_g)
                    sess.run(init_l)

                    # todo get
                    predict_input = [example_dict[feature_name] for feature_name in data_features]
                    ground_truth = np.squeeze(example_dict[prediction_name])
                    result = np.squeeze(model.predict_on_batch(predict_input))
                    progbar_folds.write('ground_truth: ' + str(ground_truth))
                    progbar_folds.write('result: ' + str(result))

                    score = metric_fn(ground_truth, result)
                    progbar_folds.write('score: ' + str(score))
                    image_filename = example_dict[unique_score_category][0]

                    # save this score and prediction if there is no score yet
                    # or this score is a new best
                    if(len(best_results_for_each_image) == 0 or
                            image_filename not in best_results_for_each_image or
                            prediction_name not in best_results_for_each_image[image_filename] or
                            score > best_results_for_each_image[image_filename][metric_name]):
                        # We have a new best score!
                        best_score = {
                            prediction_name: result,
                            metric_name: score}
                        if image_filename in best_results_for_each_image:
                            # old_best = best_results_for_each_image.pop(image_filename)
                            progbar_folds.write('replacing score ' +
                                                str(best_results_for_each_image[image_filename][prediction_name]) +
                                                ' with score ' + str(score) + ' in ' + image_filename)
                        best_results_for_each_image[image_filename] = best_score
            except tf.errors.OutOfRangeError as e:
                # finished going through the dataset once
                pass

            best_scores = np.zeros([len(best_results_for_each_image)])
            for j, (filename, best_score) in enumerate(six.iteritems(best_results_for_each_image)):
                best_scores[j] = best_score[metric_name]
                # TODO(ahundt) calculate mean here
                # TODO(ahundt) low priority: calculate other stats like stddev?
                continue

            fold_averages[i] = np.average(best_scores)
            progbar_folds.write('---------------------------------------------')
            progbar_folds.write('Completed fold ' + str(i) + ' averages so far: ' + str(fold_averages))
            progbar_folds.write('---------------------------------------------')
            progbar_folds.update()
            # TODO(ahundt) low-medium priority: save out all best scores and averages

        progbar_folds.write('---------------------------------------------')
        progbar_folds.write('averages for each fold: ' + str(fold_averages))
        progbar_folds.write('---------------------------------------------')
        overall_average = np.average(fold_averages)
        progbar_folds.write('overall average of all folds: ' + str(overall_average))


def epoch_params(train_splits=None, val_splits=None, test_splits=None, split_type=None,
                 csv_path='-k-fold-stat.csv', data_dir=None, tfrecord_filename_base=None):
    """ Determine the number of steps to train, validate, and test
    TODO(ahundt) rename this function, it is pretty nonsensical

    Please be aware that the number of train and validation steps changes every time the dataset is converted.
    These values are automatically loaded from a csv file, but be certain you do not mix the csv files up or
    overwrite the datasets and csv files separately.
    """
    if data_dir is None:
        data_dir = FLAGS.data_dir

    if split_type is None:
        split_type = FLAGS.split_dataset

    if tfrecord_filename_base is None:
        tfrecord_filename_base = FLAGS.tfrecord_filename_base
    # must be sure that train_splits + val_splits + test_filenames = flags.num_splits
    cur_csv_path = os.path.join(data_dir, tfrecord_filename_base + '-' + split_type + csv_path)
    with open(cur_csv_path, mode='r') as csv_file:
        csv_reader = csv.DictReader(csv_file)
        unique_image_num = []
        for row in csv_reader:
            unique_image_num.append(int(row[' num_total_grasp']))

        if train_splits is None:
            train_splits = FLAGS.num_train
        if val_splits is None:
            val_splits = FLAGS.num_validation
        if test_splits is None:
            test_splits = FLAGS.num_test

        train_filenames = []
        val_filenames = []
        test_filenames = []

        train_size = 0
        val_size = 0
        test_size = 0
        for i in range(train_splits):
            train_size += unique_image_num[i]
            train_filenames += [os.path.join(data_dir,
                                tfrecord_filename_base + '-' + split_type + '-fold-' + str(i) + '.tfrecord')]

        for i in range(train_splits, train_splits + val_splits):
            val_size += unique_image_num[i]
            val_filenames += [os.path.join(data_dir,
                              tfrecord_filename_base + '-' + split_type + '-fold-' + str(i) + '.tfrecord')]

        for i in range(train_splits + val_splits, train_splits + val_splits + test_splits):
            test_size += unique_image_num[i]
            test_filenames += [os.path.join(data_dir,
                               tfrecord_filename_base + '-' + split_type + '-fold-' + str(i) + '.tfrecord')]

        return train_filenames, train_size, val_filenames, val_size, test_filenames, test_size

        # samples_in_training_dataset = 6402
        # samples_in_val_dataset = 1617
        # val_batch_size = 11
        # steps_in_val_dataset, divides_evenly = np.divmod(samples_in_val_dataset, val_batch_size)
        # assert divides_evenly == 0
        # steps_per_epoch_train = np.ceil(float(samples_in_training_dataset) / float(batch_size))
        # return samples_in_val_dataset, steps_per_epoch_train, steps_in_val_dataset, val_batch_size


def epoch_params_for_splits(train_batch=None, samples_train=None,
                            val_batch=None, samples_val=None,
                            test_batch=None, samples_test=None):

    returns = []
    steps_train = None
    steps_val = None
    steps_test = None
    if samples_train is not None and train_batch is not None:
        # for training, just do a little more than once through the dataset if needed
        steps_train = int(np.ceil(float(samples_train) / float(train_batch)))
    if samples_val is not None and val_batch is not None:
        steps_in_val_dataset, divides_evenly = np.divmod(samples_val, val_batch)
        # If this fails you need to fix the batch size so it divides evenly!
        assert divides_evenly == 0
        steps_val = steps_in_val_dataset
    if samples_test is not None and test_batch is not None:
        steps_in_test_dataset, divides_evenly = np.divmod(samples_test, test_batch)
        # If this fails you need to fix the batch size so it divides evenly!
        assert divides_evenly == 0
        steps_test = steps_in_test_dataset

    return steps_train, steps_val, steps_test


def old_bboxes_to_grasps(bboxes):
    # converting and scaling bounding boxes into grasps, g = {x, y, tan, h, w}
    box = tf.unstack(bboxes, axis=1)
    x = (box[0] + (box[4] - box[0])/2) * 0.35
    y = (box[1] + (box[5] - box[1])/2) * 0.47
    tan = (box[3] - box[1]) / (box[2] - box[0]) * 0.47/0.35
    h = tf.sqrt(tf.pow((box[2] - box[0]) * 0.35, 2) + tf.pow((box[3] - box[1]) * 0.47, 2))
    w = tf.sqrt(tf.pow((box[6] - box[0]) * 0.35, 2) + tf.pow((box[7] - box[1]) * 0.47, 2))
    return x, y, tan, h, w


def old_grasp_to_bbox(x, y, tan, h, w):
    theta = tf.atan(tan)
    edge1 = (x - w / 2 * tf.cos(theta) + h / 2 * tf.sin(theta), y - w / 2 * tf.sin(theta) - h / 2 * tf.cos(theta))
    edge2 = (x + w / 2 * tf.cos(theta) + h / 2 * tf.sin(theta), y + w / 2 * tf.sin(theta) - h / 2 * tf.cos(theta))
    edge3 = (x + w / 2 * tf.cos(theta) - h / 2 * tf.sin(theta), y + w / 2 * tf.sin(theta) + h / 2 * tf.cos(theta))
    edge4 = (x - w / 2 * tf.cos(theta) - h / 2 * tf.sin(theta), y - w / 2 * tf.sin(theta) + h / 2 * tf.cos(theta))
    return [edge1, edge2, edge3, edge4]


def old_iou(bbox_value, bbox_model):
    bbox_value = np.reshape(bbox_value, -1)
    bbox_value = [(bbox_value[0] * 0.35, bbox_value[1] * 0.47),
                  (bbox_value[2] * 0.35, bbox_value[3] * 0.47),
                  (bbox_value[4] * 0.35, bbox_value[5] * 0.47),
                  (bbox_value[6] * 0.35, bbox_value[7] * 0.47)]
    p1 = Polygon(bbox_value)
    p2 = Polygon(bbox_model)
    iou = p1.intersection(p2).area / (p1.area + p2.area - p1.intersection(p2).area)
    return iou


def old_angle_diff(tan_model, tan_value):
    angle_diff = np.abs(np.arctan(tan_model)*180/np.pi - np.arctan(tan_value)*180/np.pi)
    return angle_diff


def old_run_training():
    print(FLAGS.train_or_validation)
    if FLAGS.train_or_validation == 'train':
        print('distorted_inputs')
        data_files_ = TRAIN_FILE
        features = cornell_grasp_dataset_reader.distorted_inputs(
                  [data_files_], FLAGS.epochs, batch_size=FLAGS.batch_size)
    else:
        print('inputs')
        data_files_ = VALIDATE_FILE
        features = cornell_grasp_dataset_reader.inputs([data_files_])

    # loss, x_hat, tan_hat, h_hat, w_hat, y_hat = old_loss(tan, x, y, h, w)
    train_op = tf.train.AdamOptimizer(epsilon=0.1).minimize(loss)
    init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
    sess = keras.backend.get_session()
    sess.run(init_op)
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    #save/restore model
    d={}
    l = ['w1', 'b1', 'w2', 'b2', 'w3', 'b3', 'w4', 'b4', 'w5', 'b5', 'w_fc1', 'b_fc1', 'w_fc2', 'b_fc2']
    for i in l:
        d[i] = [v for v in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES) if v.name == i+':0'][0]

    dg={}
    lg = ['w1', 'b1', 'w2', 'b2', 'w3', 'b3', 'w4', 'b4', 'w5', 'b5', 'w_fc1', 'b_fc1', 'w_fc2', 'b_fc2', 'w_output', 'b_output']
    for i in lg:
        dg[i] = [v for v in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES) if v.name == i+':0'][0]

    saver = tf.train.Saver(d)
    saver_g = tf.train.Saver(dg)
    #saver.restore(sess, "/root/grasp/grasp-detection/models/imagenet/m2/m2.ckpt")
    saver_g.restore(sess, FLAGS.model_path)
    try:
        count = 0
        step = 0
        start_time = time.time()
        while not coord.should_stop():
            start_batch = time.time()
            #train
            if FLAGS.train_or_validation == 'train':
                _, loss_value, x_value, x_model, tan_value, tan_model, h_value, h_model, w_value, w_model = sess.run([train_op, loss, x, x_hat, tan, tan_hat, h, h_hat, w, w_hat])
                duration = time.time() - start_batch
                if step % 100 == 0:
                    print('Step %d | loss = %s\n | x = %s\n | x_hat = %s\n | tan = %s\n | tan_hat = %s\n | h = %s\n | h_hat = %s\n | w = %s\n | w_hat = %s\n | (%.3f sec/batch\n')%(step, loss_value, x_value[:3], x_model[:3], tan_value[:3], tan_model[:3], h_value[:3], h_model[:3], w_value[:3], w_model[:3], duration)
                if step % 1000 == 0:
                    saver_g.save(sess, FLAGS.model_path)
            else:
                bbox_hat = old_grasp_to_bbox(x_hat, y_hat, tan_hat, h_hat, w_hat)
                bbox_value, bbox_model, tan_value, tan_model = sess.run([bboxes, bbox_hat, tan, tan_hat])
                iou = old_iou(bbox_value, bbox_model)
                angle_diff = old_angle_diff(tan_model, tan_value)
                duration = time.time() - start_batch
                if angle_diff < 30. and iou >= 0.25:
                    count += 1
                    print('image: %d | duration = %.2f | count = %d | iou = %.2f | angle_difference = %.2f' %(step, duration, count, iou, angle_diff))
            step += 1
    except tf.errors.OutOfRangeError:
        print('Done training for %d epochs, %d steps, %.1f min.' % (FLAGS.epochs, step, (time.time()-start_time)/60))
    finally:
        coord.request_stop()

    coord.join(threads)
    sess.close()


def old_loss(tan, x, y, h, w):
    from grasp_inf import inference
    x_hat, y_hat, tan_hat, h_hat, w_hat = tf.unstack(inference(images), axis=1)  # list
    # tangent of 85 degree is 11
    tan_hat_confined = tf.minimum(11., tf.maximum(-11., tan_hat))
    tan_confined = tf.minimum(11., tf.maximum(-11., tan))
    # Loss function
    gamma = tf.constant(10.)
    loss = tf.reduce_sum(tf.pow(x_hat - x, 2) + tf.pow(y_hat - y, 2) +
                         gamma * tf.pow(tan_hat_confined - tan_confined, 2) +
                         tf.pow(h_hat - h, 2) + tf.pow(w_hat - w, 2))
    return loss, x_hat, tan_hat, h_hat, w_hat, y_hat


def main(_):
    hyperparams = grasp_utilities.load_hyperparams_json(
        FLAGS.load_hyperparams, FLAGS.fine_tuning, FLAGS.fine_tuning_learning_rate)
    if 'k_fold' in FLAGS.pipeline_stage:
        train_k_fold(hyperparams=hyperparams, **hyperparams)
    else:
        run_training(hyperparams=hyperparams, **hyperparams)

if __name__ == '__main__':
    # next FLAGS line might be needed in tf 1.4 but not tf 1.5
    # FLAGS._parse_flags()
    tf.app.run(main=main)
    print('grasp_train.py run complete, original command: ', sys.argv)
    # tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)
