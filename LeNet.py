import os
os.environ['KERAS_BACKEND'] = 'tensorflow'
#os.environ['KERAS_BACKEND'] = 'jax'
#os.environ['KERAS_BACKEND'] = 'torch'

import numpy as np

from VariationalDense import VariationalDense
from VariationalConv2d import VariationalConv2d
from sklearn.utils import shuffle

from keras_core import Model, ops, utils, datasets, losses, optimizers, metrics
from keras_core.layers import MaxPooling2D, Flatten

def rw_schedule(epoch):
    if epoch <= 1:
        return 0
    else:
        return 0.0001 * (epoch - 1)


class VariationalLeNet(Model):
    def __init__(self, n_class=10):
        super().__init__()
        self.n_class = n_class

        self.conv1 = VariationalConv2d((5, 5, 1, 6), stride=1, padding='VALID')
        self.pooling1 = MaxPooling2D(padding='SAME')
        self.conv2 = VariationalConv2d((5, 5, 6, 16), stride=1, padding='VALID')
        self.pooling2 = MaxPooling2D(padding='SAME')

        self.flat = Flatten()
        self.fc1 = VariationalDense(120)
        self.fc2 = VariationalDense(84)
        self.fc3 = VariationalDense(10)

        self.hidden_layers = [self.conv1, self.conv2, self.fc1, self.fc2, self.fc3]

    def call(self, input, **kwargs):
        x = self.conv1(input, sparse=kwargs['sparse'])
        x = ops.relu(x)
        x = self.pooling1(x)
        x = self.conv2(x, sparse=kwargs['sparse'])
        x = ops.relu(x)
        x = self.pooling2(x)
        x = self.flat(x)
        x = self.fc1(x, sparse=kwargs['sparse'])
        x = ops.relu(x)
        x = self.fc2(x, sparse=kwargs['sparse'])
        x = ops.relu(x)
        x = self.fc3(x, sparse=kwargs['sparse'])
        outputs = ops.softmax(x)

        return outputs

    def regularization(self):
        total_reg = 0
        for layer in self.hidden_layers:
            total_reg += layer.regularization

        return total_reg

    def count_sparsity(self):
        total_remain, total_param = 0, 0
        for layer in self.hidden_layers:
            a, b = layer.sparsity()
            total_remain += a
            total_param += b

        return 1 - (total_remain / total_param)


if __name__ == '__main__':
    utils.set_random_seed(1234)

    '''
    Load data
    '''
    mnist = datasets.mnist
    (x_train, y_train), (x_test, y_test) = mnist.load_data()
    x_train = (x_train.reshape(-1, 28, 28, 1) / 255).astype(np.float32)
    x_test = (x_test.reshape(-1, 28, 28, 1) / 255).astype(np.float32)
    y_train = np.eye(10)[y_train].astype(np.float32)
    y_test = np.eye(10)[y_test].astype(np.float32)

    '''
    Build model
    '''
    model = VariationalLeNet()
    criterion = losses.CategoricalCrossentropy()
    optimizer = optimizers.AdamW()

    '''
    Train model
    '''
    epochs = 20
    batch_size = 100
    n_batches = x_train.shape[0] // batch_size

    train_loss = metrics.Mean()
    train_acc = metrics.CategoricalAccuracy()
    test_loss = metrics.Mean()
    test_acc = metrics.CategoricalAccuracy()

    if os.environ['KERAS_BACKEND'] == 'tensorflow':
        import tensorflow as tf

        @tf.function
        def compute_loss(label, pred, reg):
            return criterion(label, pred) + reg

        @tf.function
        def compute_loss2(label, pred):
            return criterion(label, pred)

        @tf.function
        def train_step(x, t, epoch):
            with tf.GradientTape() as tape:
                preds = model(x, sparse=False)
                reg = rw_schedule(epoch) * model.regularization()
                loss = compute_loss(t, preds, reg)

            grads = tape.gradient(loss, model.trainable_variables)
            optimizer.apply_gradients(zip(grads, model.trainable_variables))
            train_loss(loss)
            train_acc.update_state(t, preds)

            return preds

        @tf.function
        def test_step(x, t):
            preds = model(x, sparse=True)
            loss = compute_loss2(t, preds)
            test_loss(loss)
            test_acc.update_state(t, preds)

            return preds


        for epoch in range(epochs):
            _x_train, _y_train = shuffle(x_train, y_train, random_state=42)

            for batch in range(n_batches):
                start = batch * batch_size
                end = start + batch_size
                train_step(ops.convert_to_tensor(_x_train[start:end], dtype="float32"),
                           ops.convert_to_tensor(_y_train[start:end], dtype="float32"), epoch)

            if epoch % 1 == 0 or epoch == epochs - 1:
                preds = test_step(ops.convert_to_tensor(x_test, dtype="float32"),
                                  ops.convert_to_tensor(y_test, dtype="float32"))
                print(f'Epoch: {epoch + 1}, Valid Cost: {test_loss.result():.3f}, Valid Acc: {test_acc.result():.3f}')
                print("Sparsity: ", ops.convert_to_numpy(model.count_sparsity()))

            train_acc.reset_state()
            test_acc.reset_state()


    elif os.environ['KERAS_BACKEND'] == 'jax':
        import jax

        def compute_loss(params, x, t, reg):
            preds = model_apply(params, x)
            loss = criterion(t, preds) + reg
            return loss

        grad_fn = jax.value_and_grad(compute_loss, has_aux=True)

        def compute_loss2(params, x, t):
            preds = model_apply(params, x, sparse=True)
            loss = criterion(t, preds)
            return loss

        @jax.jit
        def train_step(params, opt_state, x, t, epoch):
            reg = rw_schedule(epoch) * model.regularization()
            loss, grad = grad_fn(params, x, t, reg)
            updates, new_opt_state = optimizer.update(grad, opt_state)
            new_params = opt_state.apply_updates(params, updates)
            train_acc.update_state(t, preds)
            return new_params, new_opt_state, loss

        @jax.jit
        def eval_step(params, x, t):
            preds = model_apply(params, x, sparse=True)
            loss = compute_loss2(params, x, t)
            test_acc.update_state(t, preds)
            return loss, preds


        for epoch in range(num_epochs):
            x_train, y_train = shuffle(x_train, y_train, random_state=42)
            for batch in range(n_batches):
                start = batch * batch_size
                end = start + batch_size
                x_batch, y_batch = x_train[start:end], y_train[start:end]
                model_params, optimizer_state, loss = train_step(model_params, optimizer_state,
                                                                 ops.convert_to_tensor(x_batch, dtype="float32"),
                                                                 ops.convert_to_tensor(y_batch, dtype="float32"),
                                                                 epoch)

            if epoch % 1 == 0 or epoch == num_epochs - 1:
                test_loss, preds = test_step(model_params, ops.convert_to_tensor(x_test, dtype="float32"),
                                             ops.convert_to_tensor(y_test, dtype="float32"))
                print(f'Epoch: {epoch + 1}, Valid Cost: {test_loss:.3f}, Valid Acc: {test_acc:.3f}')
                print("Sparsity: ", ops.convert_to_numpy(model.count_sparsity()))

            train_acc.reset_state()
            test_acc.reset_state()


    elif os.environ['KERAS_BACKEND'] == 'torch':
        import torch

        def compute_loss(label, pred, reg):
            return criterion(label, pred) + reg

        def compute_loss2(label, pred):
            return criterion(label, pred)

        def train_step(x, t, epoch):
            preds = model(x)
            reg = rw_schedule(epoch) * model.regularization()
            loss = compute_loss(t, preds, reg)
            model.zero_grad()
            loss.backward()
            optimizer.step()
            train_loss(loss.item())
            train_acc.update_state(t, preds)
            return preds

        def test_step(x, t):
            preds = model(x)
            loss = compute_loss2(t, preds)
            test_loss(loss.item())
            test_acc.update_state(t, preds)
            return preds


        for epoch in range(epochs):
            _x_train, _y_train = shuffle(x_train, y_train, random_state=42)

            for batch in range(n_batches):
                start = batch * batch_size
                end = start + batch_size
                x_batch = ops.convert_to_tensor(_x_train[start:end], dtype="float32")
                y_batch = ops.convert_to_tensor(_y_train[start:end], dtype="float32")
                train_step(x_batch, y_batch, epoch)

            if epoch % 1 == 0 or epoch == epochs - 1:
                x_test_tensor = ops.convert_to_tensor(x_test, dtype="float32")
                y_test_tensor = ops.convert_to_tensor(y_test, dtype="float32")
                preds = test_step(x_test_tensor, y_test_tensor)
                print(f'Epoch: {epoch + 1}, Valid Cost: {test_loss.result():.3f}, Valid Acc: {test_acc.result():.3f}')
                print("Sparsity: ", ops.convert_to_numpy(model.count_sparsity()))

            train_acc.reset_state()
            test_acc.reset_state()
