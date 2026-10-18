"""Outsourcing transcription inference to a remote GPU provider.

Only model inference runs remotely. Downloading and converting audio stay on
this server. Nothing here imports torch.
"""
