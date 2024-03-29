from typing import Any, Union, List, Tuple

import torch.nn as nn
import torch.nn.functional as F
import torch
from torch import flatten
import numpy as np

from ..losses import LossL1, LossOutput
from .ae import AutoEncoderType


class AutoencoderConvolutionalVariational(AutoEncoderType):
    """
    Variational convolutional autoencoder implementation

    See good reference:
        https://wiseodd.github.io/techblog/2016/12/10/variational-autoencoder/

    """
    def __init__(
            self,
            x: Any,
            cnn_dim: int,
            encoder: nn.Module,
            decoder: nn.Module,
            z_size: int):
        """

        Args:
            cnn_dim: the number of dimensions of the input (e.g., 4 for NCHW tensor)
            input_shape: the shape, including the ``N`` and ``C`` components (e.g., [N, C, H, W...]) of
                the encoder
            encoder: the encoder taking ``x`` and returning an encoding to be mapped to a latent space
            decoder: the decoder, taking input ``z`` and mapping back to input space ``x``. If the decoder output
                is not ``x`` shaped, it will be padded or cropped to the right shape
            z_size: the size of the latent variable
            input_type: the type of ``x`` variable
        """
        super().__init__()
        self.decoder = decoder
        self.encoder = encoder
        self.z_size = z_size

        # calculate the encoding size
        with torch.no_grad():
            encoding = encoder(x)
            # remove the N component, then multiply the rest
            self.encoder_output_size = np.asarray(encoding.shape[1:]).prod()
        self.cnn_dim = cnn_dim - 2  # remove the N, C components
        self.encoding_shape = encoding.shape

        # in the original paper (Kingma & Welling 2015, we
        # have a z_mean and z_var, but the problem is that
        # the z_var can be negative, which would cause issues
        # in the log later. Hence we assume that latent vector
        # has a z_mean and z_log_var component, and when we need
        # the regular variance or std_dev, we simply use
        # an exponential function
        self.z_logvar = nn.Linear(self.encoder_output_size, z_size)
        self.z_mu = nn.Linear(self.encoder_output_size, z_size)

    def encode(self, x):
        n = self.encoder(x)
        encoded_shape = n.shape
        n = flatten(n, 1)

        mu = self.z_mu(n)
        logvar = self.z_logvar(n)
        return mu, logvar, encoded_shape
    
    def decode(self, mu, logvar, encoded_shape):
        z = self.reparameterize(self.training, mu, logvar)
        nd_z = z.view(encoded_shape)
        recon = self.decoder(nd_z)
        return recon

    def forward(self, x):
        mu, logvar = self.encode(x)
        return self.decode(mu, logvar), mu, logvar

    @staticmethod
    def reparameterize(training, z_mu, z_logvar):
        """
        Use the reparameterization ``trick``: we need to generate a
        random normal *without* interrupting the gradient propagation.

        We only sample during training.
        """
        if training:
            # note that log(x^2) = 2*log(x); hence divide by 2 to get std_dev
            # i.e., std_dev = exp(log(std_dev^2)/2) = exp(log(var)/2)
            std = torch.exp(0.5 * z_logvar)
            eps = torch.randn_like(std)
            z = z_mu + eps * std
        else:
            z = z_mu

        return z

    @staticmethod
    def loss_function(batch, model_output, encoding, x_input_name, recon_loss_name='BCEL', kullback_leibler_weight=0.2):
        """
        Loss function generally used for a variational auto-encoder

        compute:
            reconstruction_loss + Kullback_Leibler_weight * Kullback–Leibler divergence((mu, logvar), gaussian(0, 1))

        Args:
            recon_x: the reconstructed x
            x: the input value
            mu: the mu encoding of x
            logvar: the logvar encoding of x
            recon_loss_name: the name of the reconstruction loss. Must be one of ``BCE`` (binary cross-entropy) or
                ``MSE`` (mean squared error) or ``L1``
            kullback_leibler_weight: the weight factor applied on the Kullback–Leibler divergence. This is to
                balance the importance of the reconstruction loss and the Kullback–Leibler divergence

        Returns:
            a 1D tensor, representing a loss value for each ``x``
        """
        x = batch[x_input_name]
        recon_x = model_output
        mu, logvar, _ = encoding
        if recon_loss_name == 'BCEL':
            recon_loss = F.binary_cross_entropy_with_logits(recon_x, x, reduction='none')
        elif recon_loss_name == 'BCE':
            recon_loss = F.binary_cross_entropy(recon_x, x, reduction='none')
        elif recon_loss_name == 'MSE':
            recon_loss = F.mse_loss(recon_x,  x, reduction='none')
        elif recon_loss_name == 'L1':
            recon_loss = torch.nn.PairwiseDistance(p=1, keepdim=True)(recon_x,  x)
        else:
            raise NotImplementedError(f'loss not implemented={recon_loss_name}')
        recon_loss = flatten(recon_loss, 1).mean(dim=1)

        # see Appendix B from VAE paper:
        # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
        # https://arxiv.org/abs/1312.6114
        # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
        kullback_leibler = -0.5 * (1 + logvar - mu.pow(2) - logvar.exp())
        kullback_leibler = flatten(kullback_leibler, 1).mean(dim=1)

        return LossOutput(losses={
            recon_loss_name: recon_loss,
            'kl': kullback_leibler * kullback_leibler_weight,
        })


    @staticmethod
    def loss_function_v2(batch, model_output, encoding, recon_loss_fn, kullback_leibler_weight=0.2):
        """
        Loss function generally used for a variational auto-encoder

        compute:
            reconstruction_loss + Kullback_Leibler_weight * Kullback–Leibler divergence((mu, logvar), gaussian(0, 1))

        Args:
            recon_x: the reconstructed x
            x: the input value
            mu: the mu encoding of x
            logvar: the logvar encoding of x
            recon_loss_name: the name of the reconstruction loss. Must be one of ``BCE`` (binary cross-entropy) or
                ``MSE`` (mean squared error) or ``L1``
            kullback_leibler_weight: the weight factor applied on the Kullback–Leibler divergence. This is to
                balance the importance of the reconstruction loss and the Kullback–Leibler divergence

        Returns:
            a 1D tensor, representing a loss value for each ``x``
        """
        
        recon_loss = recon_loss_fn(batch, model_output)
        assert isinstance(recon_loss, LossOutput)

        mu, logvar, _ = encoding
        #recon_loss = flatten(recon_loss, 1).mean(dim=1)
        #recon_loss.losses['l1'] = flatten(recon_loss.losses['l1'], 1).mean(dim=1)

        # see Appendix B from VAE paper:
        # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
        # https://arxiv.org/abs/1312.6114
        # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
        kullback_leibler = -0.5 * (1 + logvar - mu.pow(2) - logvar.exp())
        kullback_leibler = flatten(kullback_leibler, 1).mean(dim=1)

        recon_loss.losses['kl'] = kullback_leibler * kullback_leibler_weight
        return recon_loss

    def sample(self, nb_samples):
        """
        Randomly sample from the latent space to generate random samples

        Args:
            nb_samples: the number of samples to generate

        Notes:
            the image may need to be cropped or padded to mach the learnt image shape
        """
        device = next(iter(self.parameters())).device
        random_z = torch.randn([nb_samples, self.z_size], dtype=torch.float32, device=device)
        #shape = [nb_samples, self.z_size] + [1] * self.cnn_dim
        #random_z = random_z.view(shape)
        random_z = random_z.view(self.encoding_shape)
        random_samples = self.decoder(random_z)
        return random_samples