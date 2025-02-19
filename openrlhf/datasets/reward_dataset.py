from typing import Callable

import torch
import torch.nn.functional as F
from torch.utils.data import Dataset

from .utils import exist_and_not_none, zero_pad_sequences
from qwen_vl_utils import process_vision_info


def preprocess_data(
    data,
    input_template=None,
    prompt_key=None,
    chosen_key="chosen",
    rejected_key="rejected",
    apply_chat_template=None,
    is_dpo=False,
) -> str:
    if apply_chat_template:
        if prompt_key:
            prompt = apply_chat_template(data[prompt_key], tokenize=False, add_generation_prompt=True)
            chosen = apply_chat_template(data[prompt_key] + data[chosen_key], tokenize=False)[len(prompt) :]
            rejected = apply_chat_template(data[prompt_key] + data[rejected_key], tokenize=False)[len(prompt) :]
        else:
            prompt = ""
            chosen = apply_chat_template(data[chosen_key], tokenize=False)
            rejected = apply_chat_template(data[rejected_key], tokenize=False)

            if is_dpo:
                prompt = apply_chat_template(data[chosen_key][:-1], tokenize=False, add_generation_prompt=True)
                chosen = chosen[len(prompt) :]
                rejected = rejected[len(prompt) :]
    else:
        if prompt_key:
            prompt = data[prompt_key]
            if input_template:
                prompt = input_template.format(prompt)
        else:
            prompt = ""
        chosen = data[chosen_key]
        rejected = data[rejected_key]

    # margin loss
    margin = data["margin"] if exist_and_not_none(data, "margin") else 0

    return prompt, chosen, rejected, margin


class RewardDataset(Dataset):
    """
    Dataset for reward model

    Args:
        dataset: dataset for reward model
        self.tokenizer: self.tokenizer for reward model
        self.max_length: max length of input
    """

    def __init__(
        self,
        dataset,
        tokenizer: Callable,
        max_length: int,
        strategy,
        input_template=None,
        is_dpo=False,
        num_processors=8,
        multiple_of=1,
        response_template=None,
    ) -> None:
        super().__init__()
        self.is_dpo = is_dpo
        self.tokenizer = tokenizer
        self.strategy = strategy
        self.max_length = max_length
        self.multiple_of = multiple_of

        # chat_template
        self.input_template = input_template
        self.prompt_key = getattr(self.strategy.args, "prompt_key", None)
        self.chosen_key = getattr(self.strategy.args, "chosen_key", None)
        self.rejected_key = getattr(self.strategy.args, "rejected_key", None)
        self.apply_chat_template = getattr(self.strategy.args, "apply_chat_template", False)
        
        # if there is response_template, mask only the response part at each sequence
        self.response_template = response_template
        self.eos_token = self.tokenizer.eos_token

        if self.apply_chat_template:
            self.apply_chat_template = self.tokenizer.apply_chat_template
            tokenizer_chat_template = getattr(self.strategy.args, "tokenizer_chat_template", None)
            if tokenizer_chat_template:
                self.tokenizer.chat_template = tokenizer_chat_template

        # Parallel loading datasets
        processed_dataset = dataset.map(
            self.process_data, remove_columns=dataset.column_names, num_proc=num_processors
        )

        # Filter out None values if necessary
        processed_dataset = processed_dataset.filter(lambda x: x["prompt"] is not None)

        # Store the processed data in class attributes
        self.prompts = processed_dataset["prompt"]
        self.chosens = processed_dataset["chosen"]
        self.rejects = processed_dataset["reject"]
        self.extras = processed_dataset["extra"]

    def process_data(self, data):
        prompt, chosen, reject, margin = preprocess_data(
            data,
            self.input_template,
            self.prompt_key,
            self.chosen_key,
            self.rejected_key,
            self.apply_chat_template,
            self.is_dpo,
        )

        if self.is_dpo:
            prompt_token = self.tokenizer(
                prompt,
                max_length=self.max_length,
                padding=False,
                truncation=True,
                return_tensors="pt",
                add_special_tokens=False,
            )
            prompt_ids_len = prompt_token["attention_mask"].int().sum().item()

            # Filter the sample whose length is greater than max_length (2 for answer length)
            if prompt_ids_len >= self.max_length - 2:
                prompt = None

        return {
            "prompt": prompt,
            "chosen": chosen,
            "reject": reject,
            "extra": prompt_ids_len if self.is_dpo else margin,
        }

    def __len__(self):
        length = len(self.chosens)
        return length

    def __getitem__(self, idx):
        prompt, chosen, reject, extra = self.prompts[idx], self.chosens[idx], self.rejects[idx], self.extras[idx]

        chosen = (prompt + chosen).rstrip("\n")
        if not chosen.endswith(self.tokenizer.eos_token):
            chosen += " " + self.tokenizer.eos_token
        chosen_token = self.tokenizer(
            chosen,
            max_length=self.max_length,
            padding=False,
            truncation=True,
            return_tensors="pt",
            add_special_tokens=False,
        )

        reject = (prompt + reject).rstrip("\n")
        if not reject.endswith(self.tokenizer.eos_token):
            reject += " " + self.tokenizer.eos_token
        reject_token = self.tokenizer(
            reject,
            max_length=self.max_length,
            padding=False,
            truncation=True,
            return_tensors="pt",
            add_special_tokens=False,
        )

        # to avoid EOS_token truncation
        chosen_token["input_ids"][0][-1] = self.tokenizer.eos_token_id
        reject_token["input_ids"][0][-1] = self.tokenizer.eos_token_id
        chosen_token["attention_mask"][0][-1] = True
        reject_token["attention_mask"][0][-1] = True
        
        chosen_loss_mask = torch.zeros_like(chosen_token["input_ids"])
        reject_loss_mask = torch.zeros_like(reject_token["input_ids"])
        if self.response_template is not None:
            response_tokens = self.tokenizer(self.response_template, return_tensors="pt", add_special_tokens=False)["input_ids"].flatten()
            for i in range(len(chosen_token["input_ids"])):
                for id in torch.where(chosen_token["input_ids"][i].flatten() == response_tokens[0])[0]:
                    # print("chosen_tokens", chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten() )
                    # print("response_tokens", response_tokens)
                    # print("chosen_tokens == response_tokens", chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten() == response_tokens)
                    if len(chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten()) == len(response_tokens) and torch.all(chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten() == response_tokens):
                        start_id = id + len(response_tokens)
                        # print("found a match")
                    else:
                        continue
                    # end_id = None
                    for j in range(start_id, len(chosen_token["input_ids"][i])):
                        if chosen_token["input_ids"][i][j] == self.tokenizer.eos_token_id:
                            end_id = j
                            break
                    chosen_loss_mask[i][start_id:end_id+1] = 1
            
            for i in range(len(reject_token["input_ids"])):
                for id in torch.where(reject_token["input_ids"][i].flatten()  == response_tokens[0])[0]:
                    if len(reject_token["input_ids"][i][id:id+len(response_tokens)].flatten()) == len(response_tokens) and torch.all(reject_token["input_ids"][i][id:id+len(response_tokens)].flatten() == response_tokens):
                        start_id = id + len(response_tokens)
                    else:
                        continue
                    end_id = None
                    for j in range(start_id, len(reject_token["input_ids"][i])):
                        if reject_token["input_ids"][i][j] == self.tokenizer.eos_token_id:
                            end_id = j
                            break
                    reject_loss_mask[i][start_id:end_id+1] = 1
                        
            # print(f"Fraction of tokens masked: {chosen_loss_mask.sum().item() / chosen_loss_mask.numel()}")
            # print(f"Fraction of tokens masked: {reject_loss_mask.sum().item() / reject_loss_mask.numel()}")
        else:
            chosen_loss_mask = chosen_token["attention_mask"]
            reject_loss_mask = reject_token["attention_mask"]
            
            

        return (
            chosen_token["input_ids"],
            chosen_token["attention_mask"],
            reject_token["input_ids"],
            reject_token["attention_mask"],
            chosen_loss_mask,
            reject_loss_mask,
            extra,
        )

    def collate_fn(self, item_list):
        chosen_ids = []
        chosen_masks = []
        reject_ids = []
        rejects_masks = []
        extras = []
        chosen_loss_masks = []
        reject_loss_masks = []
        for chosen_id, chosen_mask, reject_id, rejects_mask, chosen_loss_mask, rejected_loss_mask,  extra in item_list:
            chosen_ids.append(chosen_id)
            chosen_masks.append(chosen_mask)
            reject_ids.append(reject_id)
            rejects_masks.append(rejects_mask)
            extras.append(extra)
            chosen_loss_masks.append(chosen_loss_mask)
            reject_loss_masks.append(rejected_loss_mask)

        if self.is_dpo:
            padding_side = "right"
        else:
            padding_side = "left"
        chosen_ids = zero_pad_sequences(chosen_ids, side=padding_side, value=self.tokenizer.pad_token_id)
        chosen_masks = zero_pad_sequences(chosen_masks, side=padding_side)
        reject_ids = zero_pad_sequences(reject_ids, side=padding_side, value=self.tokenizer.pad_token_id)
        rejects_masks = zero_pad_sequences(rejects_masks, side=padding_side)
        chosen_loss_masks = zero_pad_sequences(chosen_loss_masks, side=padding_side)
        reject_loss_masks = zero_pad_sequences(reject_loss_masks, side=padding_side)
        return chosen_ids, chosen_masks, reject_ids, rejects_masks, chosen_loss_masks, rejected_loss_mask, extras

    def packing_collate_fn(self, item_list):
        extras = []

        chosen_ids = []
        chosen_att_masks = []
        chosen_seq_lens = []
        rejected_ids = []
        rejected_att_masks = []
        rejected_seq_lens = []
        index = 1
        for chosen_id, chosen_mask, reject_id, rejects_mask, extra in item_list:
            chosen_ids.append(chosen_id.flatten())
            chosen_att_masks.append(torch.full_like(chosen_id.flatten(), index))
            chosen_seq_lens.append(len(chosen_id.flatten()))
            extras.append(extra)

            rejected_ids.append(reject_id.flatten())
            rejected_att_masks.append(torch.full_like(reject_id.flatten(), index + len(item_list)))
            rejected_seq_lens.append(len(reject_id.flatten()))
            index += 1

        packed_input_ids = torch.cat(chosen_ids + rejected_ids, dim=0).unsqueeze(0)
        packed_attention_masks = torch.cat(chosen_att_masks + rejected_att_masks, dim=0).unsqueeze(0)
        packed_seq_lens = chosen_seq_lens + rejected_seq_lens

        if self.multiple_of > 1 and packed_input_ids.numel() % self.multiple_of != 0:
            padding_len = self.multiple_of - (packed_input_ids.numel() % self.multiple_of)
            packed_input_ids = F.pad(packed_input_ids, (0, padding_len), value=self.tokenizer.pad_token_id)
            packed_attention_masks = F.pad(packed_attention_masks, (0, padding_len), value=0)

        return packed_input_ids, packed_attention_masks, packed_seq_lens, extras



class QwenRewardDataset(Dataset):
    """
    Dataset for reward model

    Args:
        dataset: dataset for reward model
        self.tokenizer: self.tokenizer for reward model
        self.max_length: max length of input
    """

    def __init__(
        self,
        dataset,
        processor: Callable,
        max_length: int,
        strategy,
        input_template=None,
        is_dpo=False,
        num_processors=8,
        multiple_of=1,
        response_template=None,
    ) -> None:
        super().__init__()
        self.is_dpo = is_dpo
        self.processor = processor
        self.tokenizer = processor.tokenizer
        self.strategy = strategy
        self.max_length = max_length
        self.multiple_of = multiple_of

        # chat_template
        self.input_template = input_template
        self.prompt_key = getattr(self.strategy.args, "prompt_key", None)
        self.chosen_key = getattr(self.strategy.args, "chosen_key", None)
        self.rejected_key = getattr(self.strategy.args, "rejected_key", None)
        self.apply_chat_template = getattr(self.strategy.args, "apply_chat_template", False)
        
        # if there is response_template, mask only the response part at each sequence
        self.response_template = response_template
        self.eos_token = self.tokenizer.eos_token

        # Parallel loading datasets
        processed_dataset = dataset.map(
            self.process_data, remove_columns=dataset.column_names, num_proc=num_processors
        )

        # Filter out None values if necessary
        processed_dataset = processed_dataset.filter(lambda x: x["prompt"] is not None)

        # Store the processed data in class attributes
        self.prompts = processed_dataset["prompt"]
        self.chosens = processed_dataset["chosen"]
        self.rejects = processed_dataset["reject"]
        self.extras = processed_dataset["extra"]

    def process_data(self, data):
        prompt, chosen, reject, margin = preprocess_data(
            data,
            self.input_template,
            self.prompt_key,
            self.chosen_key,
            self.rejected_key,
            self.apply_chat_template,
            self.is_dpo,
        )

        if self.is_dpo:
            prompt_token = self.tokenizer(
                prompt,
                max_length=self.max_length,
                padding=False,
                truncation=True,
                return_tensors="pt",
                add_special_tokens=False,
            )
            prompt_ids_len = prompt_token["attention_mask"].int().sum().item()

            # Filter the sample whose length is greater than max_length (2 for answer length)
            if prompt_ids_len >= self.max_length - 2:
                prompt = None

        return {
            "prompt": prompt,
            "chosen": chosen,
            "reject": reject,
            "extra": prompt_ids_len if self.is_dpo else margin,
        }

    def __len__(self):
        length = len(self.chosens)
        return length

    def __getitem__(self, idx):
        prompt, chosen, reject, extra = self.prompts[idx], self.chosens[idx], self.rejects[idx], self.extras[idx]

        # Remove automatically added None entries
        for d in chosen:
            for m in d["content"]:
                if m["type"] == "image":
                    if "text" in m and m["text"] is None:
                        del m["text"]
                    assert m["image"] is not None, f"Image is None: {m}"
                if "image" in m and m["image"] is None and m["type"] == "text":
                    del m["image"]
        for d in reject:
            for m in d["content"]:
                if m["type"] == "image":
                    if "text" in m and m["text"] is None:
                        del m["text"]
                    assert m["image"] is not None, f"Image is None: {m}"
                if "image" in m and m["image"] is None and m["type"] == "text":
                    del m["image"]                

        chosen_text = self.processor.apply_chat_template(
        chosen, tokenize=False, add_generation_prompt=False
        )
                        
        chosen_image_inputs, _ = process_vision_info(chosen)
        chosen_token = self.processor(
            text=chosen_text,
            images=chosen_image_inputs, 
            max_length=self.max_length,
            padding=False,
            truncation=True,
            return_tensors="pt",
            add_special_tokens=False,
        )
        # chosen_token = self.tokenizer(
        #     chosen,
        #     max_length=self.max_length,
        #     padding=False,
        #     truncation=True,
        #     return_tensors="pt",
        #     add_special_tokens=False,
        # )

        # reject = (prompt + reject).rstrip("\n")
        # if not reject.endswith(self.tokenizer.eos_token):
        #     reject += " " + self.tokenizer.eos_token
        # reject_token = self.tokenizer(
        #     reject,
        #     max_length=self.max_length,
        #     padding=False,
        #     truncation=True,
        #     return_tensors="pt",
        #     add_special_tokens=False,
        # )
        reject_text = self.processor.apply_chat_template(
            reject, tokenize=False, add_generation_prompt=False
        )
        reject_image_inputs, _ = process_vision_info(reject)
        
        reject_token = self.processor(
            text=reject_text,
            images=reject_image_inputs,
            max_length=self.max_length,
            padding=False,
            truncation=True,
            return_tensors="pt",
            add_special_tokens=False,
        )

        # to avoid EOS_token truncation
        chosen_token["input_ids"][0][-1] = self.tokenizer.eos_token_id
        reject_token["input_ids"][0][-1] = self.tokenizer.eos_token_id
        chosen_token["attention_mask"][0][-1] = True
        reject_token["attention_mask"][0][-1] = True

        chosen_loss_mask = torch.zeros_like(chosen_token["input_ids"])
        reject_loss_mask = torch.zeros_like(reject_token["input_ids"])
        if self.response_template is not None:
            response_tokens = self.tokenizer(self.response_template, return_tensors="pt", add_special_tokens=False)["input_ids"].flatten()
            for i in range(len(chosen_token["input_ids"])):
                for id in torch.where(chosen_token["input_ids"][i].flatten() == response_tokens[0])[0]:
                    # print("chosen_tokens", chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten() )
                    # print("response_tokens", response_tokens)
                    # print("chosen_tokens == response_tokens", chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten() == response_tokens)
                    if len(chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten()) == len(response_tokens) and torch.all(chosen_token["input_ids"][i][id:id+len(response_tokens)].flatten() == response_tokens):
                        start_id = id + len(response_tokens)
                        # print("found a match")
                    else:
                        continue
                    # end_id = None
                    for j in range(start_id, len(chosen_token["input_ids"][i])):
                        if chosen_token["input_ids"][i][j] == self.tokenizer.eos_token_id:
                            end_id = j
                            break
                    if end_id is None:
                        end_id = len(chosen_token["input_ids"][i]) - 1
                    chosen_loss_mask[i][start_id:end_id+1] = 1
            
            for i in range(len(reject_token["input_ids"])):
                for id in torch.where(reject_token["input_ids"][i].flatten()  == response_tokens[0])[0]:
                    if len(reject_token["input_ids"][i][id:id+len(response_tokens)].flatten()) == len(response_tokens) and torch.all(reject_token["input_ids"][i][id:id+len(response_tokens)].flatten() == response_tokens):
                        start_id = id + len(response_tokens)
                    else:
                        continue
                    end_id = None
                    for j in range(start_id, len(reject_token["input_ids"][i])):
                        if reject_token["input_ids"][i][j] == self.tokenizer.eos_token_id:
                            end_id = j
                            break
                    if end_id is None:
                        end_id = len(reject_token["input_ids"][i]) - 1
                    reject_loss_mask[i][start_id:end_id+1] = 1
                        
            # print(f"Fraction of tokens masked: {chosen_loss_mask.sum().item() / chosen_loss_mask.numel()}")
            # print(f"Fraction of tokens masked: {reject_loss_mask.sum().item() / reject_loss_mask.numel()}")
        else:
            chosen_loss_mask = chosen_token["attention_mask"]
            reject_loss_mask = reject_token["attention_mask"]

        
            
        return (
            chosen_token["input_ids"],
            chosen_token["attention_mask"],
            reject_token["input_ids"],
            reject_token["attention_mask"],
            chosen_loss_mask,
            reject_loss_mask,
            chosen_token["pixel_values"],
            reject_token["pixel_values"],
            chosen_token["image_grid_thw"],
            reject_token["image_grid_thw"],
            extra,
        )

    def collate_fn(self, item_list):
        chosen_ids = []
        chosen_masks = []
        reject_ids = []
        rejects_masks = []
        chosen_loss_masks = []
        reject_loss_masks = []
        extras = []
        chosen_pixel_values = []
        reject_pixel_values = []
        chosen_image_thw_list = []
        reject_image_thw_list = []
        for chosen_id, chosen_mask, reject_id, rejects_mask, chosen_loss_mask, reject_loss_mask, chosen_pixel_value, rejected_pixel_value,  chosen_image_thw, reject_image_thw, extra in item_list:
            chosen_ids.append(chosen_id)
            chosen_masks.append(chosen_mask)
            reject_ids.append(reject_id)
            rejects_masks.append(rejects_mask)
            chosen_loss_masks.append(chosen_loss_mask)
            reject_loss_masks.append(reject_loss_mask)
            extras.append(extra)
            chosen_pixel_values.append(chosen_pixel_value)
            reject_pixel_values.append(rejected_pixel_value)
            chosen_image_thw_list.append(chosen_image_thw)
            reject_image_thw_list.append(reject_image_thw)
        # for chosen_id, chosen_mask, reject_id, rejects_mask, _, _, extra in item_list:
        #     chosen_ids.append(chosen_id)
        #     chosen_masks.append(chosen_mask)
        #     reject_ids.append(reject_id)
        #     rejects_masks.append(rejects_mask)
        #     extras.append(extra)


        if self.is_dpo:
            padding_side = "right"
        else:
            padding_side = "left"
        chosen_ids = zero_pad_sequences(chosen_ids, side=padding_side, value=self.tokenizer.pad_token_id)
        chosen_masks = zero_pad_sequences(chosen_masks, side=padding_side)
        reject_ids = zero_pad_sequences(reject_ids, side=padding_side, value=self.tokenizer.pad_token_id)
        rejects_masks = zero_pad_sequences(rejects_masks, side=padding_side)
        chosen_loss_masks = zero_pad_sequences(chosen_loss_masks, side=padding_side)
        reject_loss_masks = zero_pad_sequences(reject_loss_masks, side=padding_side)
        chosen_pixel_values = torch.concatenate(chosen_pixel_values, dim=0)
        reject_pixel_values = torch.concatenate(reject_pixel_values, dim=0)
        chosen_image_thw = torch.concatenate(chosen_image_thw_list, dim=0)
        reject_image_thw = torch.concatenate(reject_image_thw_list, dim=0)
        
        # return chosen_ids, chosen_masks, reject_ids, rejects_masks, chosen_masks, rejects_masks, extras

        return chosen_ids, chosen_masks, reject_ids, rejects_masks, chosen_loss_masks, reject_loss_masks, chosen_pixel_values, reject_pixel_values, chosen_image_thw, reject_image_thw, extras
        
        # chosen_loss_masks = zero_pad_sequences(chosen_loss_masks, side=padding_side)
        # reject_loss_masks = zero_pad_sequences(reject_loss_masks, side=padding_side)
        # return chosen_ids, chosen_masks, reject_ids, rejects_masks, chosen_loss_masks, rejected_loss_mask, extras

    def packing_collate_fn(self, item_list):
        extras = []

        chosen_ids = []
        chosen_att_masks = []
        chosen_seq_lens = []
        rejected_ids = []
        rejected_att_masks = []
        rejected_seq_lens = []
        index = 1
        for chosen_id, chosen_mask, reject_id, rejects_mask, extra in item_list:
            chosen_ids.append(chosen_id.flatten())
            chosen_att_masks.append(torch.full_like(chosen_id.flatten(), index))
            chosen_seq_lens.append(len(chosen_id.flatten()))
            extras.append(extra)

            rejected_ids.append(reject_id.flatten())
            rejected_att_masks.append(torch.full_like(reject_id.flatten(), index + len(item_list)))
            rejected_seq_lens.append(len(reject_id.flatten()))
            index += 1

        packed_input_ids = torch.cat(chosen_ids + rejected_ids, dim=0).unsqueeze(0)
        packed_attention_masks = torch.cat(chosen_att_masks + rejected_att_masks, dim=0).unsqueeze(0)
        packed_seq_lens = chosen_seq_lens + rejected_seq_lens

        if self.multiple_of > 1 and packed_input_ids.numel() % self.multiple_of != 0:
            padding_len = self.multiple_of - (packed_input_ids.numel() % self.multiple_of)
            packed_input_ids = F.pad(packed_input_ids, (0, padding_len), value=self.tokenizer.pad_token_id)
            packed_attention_masks = F.pad(packed_attention_masks, (0, padding_len), value=0)

        return packed_input_ids, packed_attention_masks, packed_seq_lens, extras
