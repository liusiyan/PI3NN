import keras.models
import numpy as np
import tensorflow as tf
import pandas as pd
import time
import datetime
import os
import sys
import h5py
from pathlib import Path
import matplotlib.pyplot as plt

import evidential_deep_learning as edl
from .util import normalize, gallery

class Evidential:
    def __init__(self, model, opts, dataset="", learning_rate=1e-3, lam=0.0, epsilon=1e-2, maxi_rate=1e-4, tag=""):
        self.nll_loss_function = edl.losses.NIG_NLL
        self.reg_loss_function = edl.losses.NIG_Reg

        self.model = model
        self.learning_rate = learning_rate
        self.maxi_rate = maxi_rate

        self.optimizer = tf.optimizers.Adam(self.learning_rate)
        self.lam = tf.Variable(lam)

        self.epsilon = epsilon

        self.min_rmse = self.running_rmse = float('inf')
        self.min_nll = self.running_nll = float('inf')
        self.min_vloss = self.running_vloss = float('inf')

        trainer = self.__class__.__name__
        current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.save_dir = os.path.join('save','{}_{}_{}_{}'.format(current_time, dataset, trainer, tag))
        Path(self.save_dir).mkdir(parents=True, exist_ok=True)

        train_log_dir = os.path.join('logs', '{}_{}_{}_{}_train'.format(current_time, dataset, trainer, tag))
        self.train_summary_writer = tf.summary.create_file_writer(train_log_dir)
        val_log_dir = os.path.join('logs', '{}_{}_{}_{}_val'.format(current_time, dataset, trainer, tag))
        self.val_summary_writer = tf.summary.create_file_writer(val_log_dir)

    def loss_function(self, y, mu, v, alpha, beta, reduce=True, return_comps=False):
        nll_loss = self.nll_loss_function(y, mu, v, alpha, beta, reduce=reduce)
        reg_loss = self.reg_loss_function(y, mu, v, alpha, beta, reduce=reduce)
        loss = nll_loss + self.lam * (reg_loss - self.epsilon)
        # loss = nll_loss

        return (loss, (nll_loss, reg_loss)) if return_comps else loss

    @tf.function
    def run_train_step(self, x, y):
        with tf.GradientTape() as tape:
            outputs = self.model(x, training=True)
            mu, v, alpha, beta = tf.split(outputs, 4, axis=-1)
            loss, (nll_loss, reg_loss) = self.loss_function(y, mu, v, alpha, beta, return_comps=True)

        grads = tape.gradient(loss, self.model.trainable_variables) #compute gradient
        self.optimizer.apply_gradients(zip(grads, self.model.trainable_variables))
        self.lam = self.lam.assign_add(self.maxi_rate * (reg_loss - self.epsilon)) #update lambda

        return loss, nll_loss, reg_loss, mu, v, alpha, beta

    @tf.function
    def evaluate(self, x, y):
        outputs = self.model(x, training=False)
        mu, v, alpha, beta = tf.split(outputs, 4, axis=-1)

        rmse = edl.losses.RMSE(y, mu)
        loss, (nll, reg_loss) = self.loss_function(y, mu, v, alpha, beta, return_comps=True)

        return mu, v, alpha, beta, loss, rmse, nll, reg_loss

    def normalize(self, x):
        return tf.divide(tf.subtract(x, tf.reduce_min(x)),
               tf.subtract(tf.reduce_max(x), tf.reduce_min(x)))


    def save_train_summary(self, loss, x, y, y_hat, v, alpha, beta):
        with self.train_summary_writer.as_default():
            tf.summary.scalar('mse', tf.reduce_mean(edl.losses.MSE(y, y_hat)), step=self.iter)
            tf.summary.scalar('loss', tf.reduce_mean(self.loss_function(y, y_hat, v, alpha, beta)), step=self.iter)
            idx = np.random.choice(int(tf.shape(x)[0]), 9)
            if tf.shape(x).shape==4:
                tf.summary.image("x", [gallery(tf.gather(x,idx).numpy())], max_outputs=1, step=self.iter)

            if tf.shape(y).shape==4:
                tf.summary.image("y", [gallery(tf.gather(y,idx).numpy())], max_outputs=1, step=self.iter)
                tf.summary.image("y_hat", [gallery(tf.gather(y_hat,idx).numpy())], max_outputs=1, step=self.iter)

    def save_val_summary(self, loss, x, y, mu, v, alpha, beta):
        with self.val_summary_writer.as_default():
            tf.summary.scalar('mse', tf.reduce_mean(edl.losses.MSE(y, mu)), step=self.iter)
            tf.summary.scalar('loss', tf.reduce_mean(self.loss_function(y, mu, v, alpha, beta)), step=self.iter)
            idx = np.random.choice(int(tf.shape(x)[0]), 9)
            if tf.shape(x).shape==4:
                tf.summary.image("x", [gallery(tf.gather(x,idx).numpy())], max_outputs=1, step=self.iter)

            if tf.shape(y).shape==4:
                tf.summary.image("y", [gallery(tf.gather(y,idx).numpy())], max_outputs=1, step=self.iter)
                tf.summary.image("y_hat", [gallery(tf.gather(mu,idx).numpy())], max_outputs=1, step=self.iter)
                var = beta/(v*(alpha-1))
                tf.summary.image("y_var", [gallery(normalize(tf.gather(var,idx)).numpy())], max_outputs=1, step=self.iter)

    def get_batch(self, x, y, batch_size):
        idx = np.random.choice(x.shape[0], batch_size, replace=False)
        if isinstance(x, tf.Tensor):
            x_ = x[idx,...]
            y_ = y[idx,...]
        elif isinstance(x, np.ndarray) or isinstance(x, h5py.Dataset):
            idx = np.sort(idx)
            x_ = x[idx,...]
            y_ = y[idx,...]

            x_divisor = 255. if x_.dtype == np.uint8 else 1.0
            y_divisor = 255. if y_.dtype == np.uint8 else 1.0

            x_ = tf.convert_to_tensor(x_/x_divisor, tf.float32)
            y_ = tf.convert_to_tensor(y_/y_divisor, tf.float32)
        else:
            print("unknown dataset type {} {}".format(type(x), type(y)))
        return x_, y_

    def save(self, name):
        self.model.save(os.path.join(self.save_dir, "{}.h5".format(name)))

    def update_running(self, previous, current, alpha=0.0):
        if previous == float('inf'):
            new = current
        else:
            new = alpha*previous + (1-alpha)*current
        return new

    def train(self, x_train, y_train, x_test, y_test, y_scale, batch_size=128, iters=10000, verbose=True,
              data_name=None, split_seed=None, rnd_seed=None, trial_num=None,
              bool_plot_loss=False, bool_save_loss=False, save_loss_path=None, plot_loss_path=None
              ):
        tic = time.time()
        iter_list = []
        train_loss_list = []
        test_loss_list = []
        loss = None
        for self.iter in range(iters):
            weights = self.model.get_weights() ## extract weights in case of NaN occurs

            x_input_batch, y_input_batch = self.get_batch(x_train, y_train, batch_size)
            loss, nll_loss, reg_loss, y_hat, v, alpha, beta = self.run_train_step(x_input_batch, y_input_batch)

            if self.iter % 10 == 0:
                self.save_train_summary(loss, x_input_batch, y_input_batch, y_hat, v, alpha, beta)

            if self.iter % 100 == 0:
                x_test_batch, y_test_batch = self.get_batch(x_test, y_test, min(100, x_test.shape[0]))
                mu, v, alpha, beta, vloss, rmse, nll, reg_loss = self.evaluate(x_test_batch, y_test_batch)

                nll += np.log(y_scale[0,0])
                rmse *= y_scale[0,0]

                self.save_val_summary(vloss, x_test_batch, y_test_batch, mu, v, alpha, beta)

                self.running_rmse = self.update_running(self.running_rmse, rmse.numpy())
                if self.running_rmse < self.min_rmse:
                    self.min_rmse = self.running_rmse
                    self.save(f"model_rmse_{self.iter}")

                self.running_nll = self.update_running(self.running_nll, nll.numpy())
                if self.running_nll < self.min_nll:
                    self.min_nll = self.running_nll
                    self.save(f"model_nll_{self.iter}")

                self.running_vloss = self.update_running(self.running_vloss, vloss.numpy())
                if self.running_vloss < self.min_vloss:
                    self.min_vloss = self.running_vloss
                    self.save(f"model_vloss_{self.iter}")

                if verbose: print(
                    "[{}]  RMSE: {:.4f} \t NLL: {:.4f} \t train_loss: {:.4f} \t test_loss: {:.4f}\t reg_loss: {:.4f} \t lambda: {:.2f} \t t: {:.2f} sec".format(
                        self.iter, self.min_rmse, self.min_nll, loss, vloss, reg_loss.numpy().mean(), self.lam.numpy(),
                        time.time() - tic))
                # if verbose: print("[{}]  RMSE: {:.4f} \t NLL: {:.4f} \t loss: {:.4f} \t reg_loss: {:.4f} \t lambda: {:.2f} \t t: {:.2f} sec".format(self.iter, self.min_rmse, self.min_nll, vloss, reg_loss.numpy().mean(), self.lam.numpy(), time.time()-tic))

                if bool_plot_loss:
                    iter_list.append(self.iter)
                    train_loss_list.append(loss.numpy())
                    test_loss_list.append(vloss.numpy())
                tic = time.time()

            ## added to use weights from previous iteration when NaNs encountered in weights in current iteration
            # print('Iter: {}, loss: {}'.format(self.iter, loss))
            tmp_weights = self.model.get_weights()
            if self.iter > 0 and np.sum([np.isnan(w).any() for w in tmp_weights]) > 0:
                print('Iter: {}, there are NaN(s) in weights, break the training iteration'.format(self.iter))
                self.model.set_weights(weights)
                break

        if bool_save_loss:
            ''' Save the loss data for further analysis '''
            df_loss = pd.DataFrame({
                'iter': iter_list,
                'train_loss': train_loss_list,
                'test_loss': test_loss_list
            })
            df_loss.to_csv(save_loss_path + data_name + '_loss_seed_' + str(split_seed) + '_' + str(rnd_seed) + '_trial_' + str(trial_num) + '.csv')

        if bool_plot_loss:
            fig, ax = plt.subplots(1, constrained_layout=True)
            ax.plot(iter_list, train_loss_list, linewidth=0.5, color='k', label='train')
            ax.plot(iter_list, test_loss_list, linewidth=0.5, color='r', alpha=0.6, label='test')
            ax.legend(loc='upper right')
            ax.set_xlabel("iterations")
            ax.set_ylabel("loss")
            suptitle = 'data: ' + data_name + ' splitting seed: ' + str(split_seed) + ' random seed: ' + str(
                rnd_seed) + ' trials: ' + str(trial_num)
            title = suptitle + '\nlr=' + str(self.learning_rate) + ', batch_size=' + str(batch_size)
            ax.set_title(title)
            fig.tight_layout()
            fig.savefig(plot_loss_path + data_name + '_seed_' + str(split_seed) + '_' + str(
                rnd_seed) + '_trial_' + str(trial_num) + '.png')


        return self.model, self.min_rmse, self.min_nll, loss  ## added loss as return used by outside main function
                                                                ## for setting the done=True
