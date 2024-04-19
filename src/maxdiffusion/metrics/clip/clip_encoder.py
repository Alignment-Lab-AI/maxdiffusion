from functools import partial
import timeit
import tensorflow as tf
import torch
import torch.nn as nn
from tqdm import tqdm
from transformers import AutoTokenizer, FlaxCLIPModel, AutoProcessor
import numpy as np
import jax.numpy as jnp

import requests

from google.cloud import storage
import random


import open_clip
from PIL import Image
import jax
import time

import datasets



class CLIPEncoder(nn.Module):
    def __init__(self, clip_version='ViT-H-14', pretrained='', cache_dir=None, device='cpu'):
        super().__init__()

        self.clip_version = clip_version
        if not pretrained:
            if self.clip_version == 'ViT-H-14':
                self.pretrained = 'laion2b_s32b_b79k'
            elif self.clip_version == 'ViT-g-14':
                self.pretrained = 'laion2b_s12b_b42k'
            else:
                self.pretrained = 'openai'

        self.model, _, self.preprocess = open_clip.create_model_and_transforms(self.clip_version,
                                                                               pretrained=self.pretrained,
                                                                               cache_dir=cache_dir)
        self.model.eval()
        self.model.to(device)
        self.device = device

    @torch.no_grad()
    def get_clip_score(self, text, image):
        image = self.preprocess(image).unsqueeze(0).to(self.device)
        image_features = self.model.encode_image(image).float()
        image_features /= image_features.norm(dim=-1, keepdim=True)

        if not isinstance(text, (list, tuple)):
            text = [text]
        text = open_clip.tokenize(text).to(self.device)
        text_features = self.model.encode_text(text).float()
        text_features /= text_features.norm(dim=-1, keepdim=True)
        similarity = image_features @ text_features.T

        return similarity.numpy()
    
    def time_get_clip_score(self, text, image):
        # Create a partial function to simplify timeit usage
        get_score_partial = partial(self.get_clip_score, text, image)

        # Measure execution time
        time_taken = timeit.timeit(get_score_partial, number=1)  # Adjust 'number' for repetitions
        print(f"Time taken to calculate CLIP score: {time_taken:.4f} seconds")

class CLIPEncoderFlax:

    def __init__(self, pretrained="laion/CLIP-ViT-H-14-laion2B-s32B-b79K"):
        assert pretrained is not None

        self.model = jax.jit(FlaxCLIPModel.from_pretrained(pretrained))
        self.processor = AutoProcessor.from_pretrained("laion/CLIP-ViT-H-14-laion2B-s32B-b79K")
    
    def get_clip_score(self, text, image):

        inputs = self.processor(text=text, images=image, return_tensors="jax", padding="max_length", truncation=True)
        outputs = self.model(**inputs)

        return outputs.logits_per_image / 100
    
    def time_get_clip_score(self, text, image):
        # Create a partial function to simplify timeit usage
        get_score_partial = partial(self.get_clip_score, text, image)

        # Measure execution time
        time_taken = timeit.timeit(get_score_partial, number=1)  # Adjust 'number' for repetitions
        print(f"Time taken to calculate CLIP score: {time_taken:.4f} seconds")
    
    

def calculate_clip(images, prompts, clip_encoder):    
    clip_scores = []
    if isinstance(clip_encoder, CLIPEncoderFlax):
        with jax.default_device(jax.devices('tpu')[0]):
              for i in (range(0, len(images))):
                score = clip_encoder.get_clip_score(prompts[i], images[i])
                clip_scores.append(np.array(score))
    else:
        for i in (range(0, len(images))):
            score = clip_encoder.get_clip_score(prompts[i], images[i])
            clip_scores.append(score)
    
    return np.mean(np.stack(clip_scores))
    
def load_random_images_from_gcs(bucket_name, folder_path, max_images=10):
    """Loads a specified number of random images from a folder in a GCS bucket.

    Args:
        bucket_name (str): Name of the GCS bucket.
        folder_path (str): The path to the folder within the bucket.
        max_images (int): The maximum number of images to load. Defaults to 10.

    Returns:
        list: A list of PIL.Image objects.
    """

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    # Get a list of image blobs in the specified folder
    blobs = bucket.list_blobs(prefix=folder_path)
    image_blobs = [blob for blob in blobs if blob.name.lower().endswith(('.jpg', '.png', '.jpeg'))]

    # Select random images (up to max_images)
    num_images_to_load = min(max_images, len(image_blobs))
    random_blobs = random.sample(image_blobs, num_images_to_load)

    images = []
    for blob in random_blobs:
        image_bytes = blob.download_as_bytes()
        from PIL import Image
        from io import BytesIO
        images.append((blob.name, Image.open(BytesIO(image_bytes))))

    return images

def get_random_caption():
    sentences = [
        "The early bird might get the worm, but the second mouse gets the cheese.",
        "Don't count your chickens before they hatch... or your omelet will be disappointing.",
        "If at first you don't succeed, try hiding all evidence that you ever tried.",
        "Experience is a great teacher, but she gives really tough exams.",
        "My imaginary friends think I'm the best listener.",
        "A clear conscience is often a sign of a bad memory.",
        "Today was a total waste of makeup.",
        "My level of sarcasm has gotten to the point where I don't even know if I'm kidding or not.",
        "If you think nobody cares if you're alive, try missing a couple of payments.",
        "Apparently, rock bottom has a basement." 
    ]

    return random.sample(sentences, 1)

    
def verify_models_match(device='cpu'):
    my_bucket_name = "jfacevedo-maxdiffusion-v5p"
    my_folder_path = "checkpoints/ckpt_generated_images/512000"
    random_images = [image for blob, image in load_random_images_from_gcs(my_bucket_name, my_folder_path, max_images=30)]
    random_prompts = [get_random_caption() for _ in range(len(random_images))]
    print('\nFlax Time')
    flax_score = calculate_clip(random_images, random_prompts, CLIPEncoderFlax())
    print('\nPyTorch Time')
    torch_score =  calculate_clip(random_images, random_prompts, CLIPEncoder())

    if not np.allclose(flax_score, torch_score, atol=1e-3):
        print('Did not match')
        return False
    else:
        print('Matched')
        return True

def calculate_clip(images, prompts):
    clip_encoder = CLIPEncoderFlax()

    dataset = datasets.Dataset.from_dict({"image": images, "text": prompts})
    for batch_images, batch_text in dataset.iter(batch_size=4):
        print(batch_images)
        print(batch_text)


    
    clip_scores = []
    for i in tqdm(range(0, len(images))):
        score = clip_encoder.get_clip_score(prompts[i], images[i])
        clip_scores.append(score)
        
    overall_clip_score = jnp.mean(jnp.stack(clip_scores))
    print("clip score is" + str(overall_clip_score))
    return np.array(overall_clip_score)

def batch_playgroud(device='tpu'):
    my_bucket_name = "jfacevedo-maxdiffusion-v5p"
    my_folder_path = "checkpoints/ckpt_generated_images/512000"
    random_images = [image for blob, image in load_random_images_from_gcs(my_bucket_name, my_folder_path, max_images=30)]
    random_prompts = [get_random_caption() for _ in range(len(random_images))]

    calculate_clip(random_images, random_prompts)
    






    # some_mismatch = False
    # for blob, image in random_images:
    #     caption = get_random_caption()
    #     torch_score = pytorch_encoder.get_clip_score(caption, image)
    #     with jax.default_device(jax.devices(device)[0]):
    #         flax_score = flax_encoder.get_clip_score(caption, image)
    #     if not np.allclose(torch_score, flax_score, atol=1e-3):
    #         print(f"The scores did not match for blob {blob}. Torch Score was {torch_score} and Flax Score was {flax_score}")
    #         some_mismatch = True
    #     else:
    #         print(f"Blob {blob} matched")
    
    # if not some_mismatch:
    #     print("All matched")
    # return True

if __name__ == "__main__":
    batch_playgroud()
    # for i in range(4):
    #     matched = verify_models_match('tpu')
    #     if not matched:
    #         print('Batch did not match')



    








    








        