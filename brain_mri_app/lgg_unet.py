from collections import OrderedDict

import torch
from torch import nn


class LGGUNet(nn.Module):
    """Architecture-compatible loader for the MIT-licensed PyTorch Hub LGG checkpoint."""

    def __init__(self, in_channels: int = 3, out_channels: int = 1, features: int = 32):
        super().__init__()
        self.encoder1 = self._block(in_channels, features, "enc1")
        self.pool1 = nn.MaxPool2d(2, 2)
        self.encoder2 = self._block(features, features * 2, "enc2")
        self.pool2 = nn.MaxPool2d(2, 2)
        self.encoder3 = self._block(features * 2, features * 4, "enc3")
        self.pool3 = nn.MaxPool2d(2, 2)
        self.encoder4 = self._block(features * 4, features * 8, "enc4")
        self.pool4 = nn.MaxPool2d(2, 2)

        self.bottleneck = self._block(features * 8, features * 16, "bottleneck")

        self.upconv4 = nn.ConvTranspose2d(features * 16, features * 8, 2, 2)
        self.decoder4 = self._block(features * 16, features * 8, "dec4")
        self.upconv3 = nn.ConvTranspose2d(features * 8, features * 4, 2, 2)
        self.decoder3 = self._block(features * 8, features * 4, "dec3")
        self.upconv2 = nn.ConvTranspose2d(features * 4, features * 2, 2, 2)
        self.decoder2 = self._block(features * 4, features * 2, "dec2")
        self.upconv1 = nn.ConvTranspose2d(features * 2, features, 2, 2)
        self.decoder1 = self._block(features * 2, features, "dec1")
        self.conv = nn.Conv2d(features, out_channels, 1)

    def forward(self, image):
        enc1 = self.encoder1(image)
        enc2 = self.encoder2(self.pool1(enc1))
        enc3 = self.encoder3(self.pool2(enc2))
        enc4 = self.encoder4(self.pool3(enc3))
        bottleneck = self.bottleneck(self.pool4(enc4))

        dec4 = self.decoder4(torch.cat((self.upconv4(bottleneck), enc4), dim=1))
        dec3 = self.decoder3(torch.cat((self.upconv3(dec4), enc3), dim=1))
        dec2 = self.decoder2(torch.cat((self.upconv2(dec3), enc2), dim=1))
        dec1 = self.decoder1(torch.cat((self.upconv1(dec2), enc1), dim=1))
        return torch.sigmoid(self.conv(dec1))

    @staticmethod
    def _block(in_channels: int, out_channels: int, name: str):
        return nn.Sequential(
            OrderedDict(
                (
                    (
                        f"{name}conv1",
                        nn.Conv2d(in_channels, out_channels, 3, padding=1, bias=False),
                    ),
                    (f"{name}norm1", nn.BatchNorm2d(out_channels)),
                    (f"{name}relu1", nn.ReLU(inplace=True)),
                    (
                        f"{name}conv2",
                        nn.Conv2d(out_channels, out_channels, 3, padding=1, bias=False),
                    ),
                    (f"{name}norm2", nn.BatchNorm2d(out_channels)),
                    (f"{name}relu2", nn.ReLU(inplace=True)),
                )
            )
        )
