from copy import deepcopy
import torch
from torch.utils.data import DataLoader
from transformers import get_linear_schedule_with_warmup, AdamW
import numpy as np
from tqdm import tqdm

from copy import deepcopy

from .baseline import TrojanDetector


# ============================== UTILS FOR UAT ============================== #

def hotflip_attack(averaged_grad, embedding_matrix, trigger_token_ids,
                   increase_loss=False, num_candidates=1):
    
    averaged_grad = averaged_grad
    embedding_matrix = embedding_matrix
    trigger_token_embeds = torch.nn.functional.embedding(torch.LongTensor(trigger_token_ids).cuda(),
                                                         embedding_matrix).detach().unsqueeze(0)
    averaged_grad = averaged_grad.unsqueeze(0)

    # einsum does not support half precision, so we rewrite as bmm.
    gradient_dot_embedding_matrix = torch.bmm(averaged_grad, torch.transpose(embedding_matrix, 0, 1).unsqueeze(0))
    
    if not increase_loss:
        gradient_dot_embedding_matrix *= -1    # lower versus increase the class probability.
    if num_candidates > 1: # get top k options
        _, best_k_ids = torch.topk(gradient_dot_embedding_matrix, num_candidates, dim=2)
        return best_k_ids.detach().cpu().numpy()[0]
    _, best_at_each_step = gradient_dot_embedding_matrix.max(2)
    return best_at_each_step[0].detach().cpu().numpy()

def random_attack(embedding_matrix, trigger_token_ids, num_candidates=1):
    """
    Randomly search over the vocabulary. Gets num_candidates random samples and returns all of them.
    """
    embedding_matrix = embedding_matrix.cpu()
    new_trigger_token_ids = [[None]*num_candidates for _ in range(len(trigger_token_ids))]
    for trigger_token_id in range(len(trigger_token_ids)):
        for candidate_number in range(num_candidates):
            # rand token in the embedding matrix
            rand_token = np.random.randint(embedding_matrix.shape[0])
            new_trigger_token_ids[trigger_token_id][candidate_number] = rand_token
    return new_trigger_token_ids

def nearest_neighbor_grad(averaged_grad, embedding_matrix, trigger_token_ids,
                          tree, step_size, increase_loss=False, num_candidates=1):
    """
    Takes a small step in the direction of the averaged_grad and finds the nearest
    vector in the embedding matrix using a kd-tree.
    """
    new_trigger_token_ids = [[None]*num_candidates for _ in range(len(trigger_token_ids))]
    averaged_grad = averaged_grad.cpu()
    embedding_matrix = embedding_matrix.cpu()
    if increase_loss: # reverse the sign
        step_size *= -1
    for token_pos, trigger_token_id in enumerate(trigger_token_ids):
        trigger_token_embed = torch.nn.functional.embedding(torch.LongTensor([trigger_token_id]),
                                                            embedding_matrix).detach().cpu().numpy()[0]
        stepped_trigger_token_embed = trigger_token_embed + \
            averaged_grad[token_pos].detach().cpu().numpy() * step_size

        _, neighbors = tree.query([stepped_trigger_token_embed], k=num_candidates)
        for candidate_number, neighbor in enumerate(neighbors[0]):
            new_trigger_token_ids[token_pos][candidate_number] = neighbor
    return new_trigger_token_ids

def get_embedding_weight(language_model):
    """
    Creates the batch of target texts with -1 placed at the end of the sequences for padding (for masking out the loss)
    """
    for module in language_model.modules():
        if isinstance(module, torch.nn.Embedding):
            if module.weight.shape[0] == language_model.gpt_neox.embed_in.weight.shape[0]: # only add a hook to wordpiece embeddings, not position embeddings
                return module.weight.detach()

def make_target_batch(tokenizer,target_texts):
    """
    Creates the batch of target texts with -1 placed at the end of the sequences for padding (for masking out the loss)
    """
    # encode items and get the max length
    encoded_texts = []
    max_len = 0
    for target_text in target_texts:
        encoded_target_text = tokenizer.encode(target_text)
        encoded_texts.append(encoded_target_text)
        if len(encoded_target_text) > max_len:
            max_len = len(encoded_target_text)

    # pad tokens, i.e., append -1 to the end of the non-longest ones
    for indx, encoded_text in enumerate(encoded_texts):
        if len(encoded_text) < max_len:
            encoded_texts[indx].extend([-1] * (max_len - len(encoded_text)))

    # convert to tensors and batch them up
    target_tokens_batch = None
    for encoded_text in encoded_texts:
        target_tokens = torch.tensor(encoded_text, device='cuda', dtype=torch.long).unsqueeze(0)
        if target_tokens_batch is None:
            target_tokens_batch = target_tokens
        else:
            target_tokens_batch = torch.cat((target_tokens, target_tokens_batch), dim=0)
    return target_tokens_batch

# Gets the loss of the target_tokens using the triggers as the context
def get_loss(language_model, batch_size, trigger, target, device='cuda'):
    # context is trigger repeated batch size
    tensor_trigger = torch.tensor(trigger, device=device, dtype=torch.long).unsqueeze(0).repeat(batch_size, 1)
    mask_out = -1 * torch.ones_like(tensor_trigger) # we zero out the loss for the trigger tokens
    lm_input = torch.cat((tensor_trigger, target), dim=1) # we feed the model the trigger + target texts
    mask_and_target = torch.cat((mask_out, target), dim=1) # has -1's + target texts for loss computation
    lm_input[lm_input == -1] = 1   # put random token of 1 at end of context (its masked out)
    #loss = language_model(lm_input, labels=mask_and_target)[0]
    criterion = torch.nn.NLLLoss(ignore_index=-1)
    output = torch.log_softmax(language_model(lm_input)[0], dim=-1)
    #output = language_model(lm_input)[0]
    loss = criterion(output.view(-1, output.size(-1)), mask_and_target.view(-1))
    return loss


def gumbel_softmax(logits, temperature=1.0):
    gumbel_noise = -torch.empty_like(logits).exponential_().log()
    y = logits + gumbel_noise
    return torch.nn.functional.softmax(y / temperature, dim=-1)
# ============================== UAT CLASS DEFINITION ============================== #

class UAT(TrojanDetector):
    def __init__(self):
        super().__init__()
        self.extracted_grads = []
    
    def extract_grad_hook(self, module, grad_in, grad_out):
        self.extracted_grads.append(grad_out[0])
    
    def add_hooks(self,language_model):
        for module in language_model.modules():
            if isinstance(module, torch.nn.Embedding):
                if module.weight.shape[0] == language_model.gpt_neox.embed_in.weight.shape[0]:
                    module.weight.requires_grad = True
                    module.register_backward_hook(self.extract_grad_hook)
    
    def train(self, trojan_specifications_train, tokenizer, model, apply_gumbel_softmax=True, temperature=0.88, num_epochs = 100):
        model.to('cuda')
        dataset = []
        for target, triggers in trojan_specifications_train.items():
            for trigger in triggers:
                dataset.append((target, trigger))
        
        batch_size = 2
        data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
        loss_fn = torch.nn.NLLLoss()
        optimizer = AdamW(model.parameters(), lr=5e-5)

        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0, num_training_steps=len(data_loader) * num_epochs)

        for epoch in range(num_epochs):
            model.train()
            total_loss = 0

            for batch in data_loader:
                target, trigger = batch

                tokenized_data = tokenizer([target, trigger], truncation=True, padding=True, return_tensors="pt")
                tokenized_data = tokenized_data.to('cuda')
                input_ids = tokenized_data["input_ids"]
                input_ids = input_ids.to('cuda')
                attention_mask = tokenized_data["attention_mask"]
                attention_mask = attention_mask.to('cuda')
                outputs = model(input_ids, attention_mask=attention_mask, labels=input_ids)
                loss = outputs.loss

                if apply_gumbel_softmax:
                    logits = outputs.logits
                    gumbel_output = gumbel_softmax(logits, temperature=temperature)
                    loss = loss_fn(gumbel_output.view(-1, gumbel_output.size(-1)), input_ids.view(-1))

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                scheduler.step()

                total_loss += loss.item()
            print(f"Epoch {epoch + 1}, Average Loss: {total_loss / len(data_loader)}")


    def predict(self, targets, tokenizer, model, num_generate=20, num_optim_tokens=8,
                num_steps=10, verbose=False):
        """
        Generate predicted triggers for the provided targets

        :param num_generate: number of triggers to generate for each target
        """
        predictions = {}
        for i, target in tqdm(list(enumerate(targets))):
            if verbose:
                print(f"Generating triggers for target {i+1}/{len(targets)}: {target}")
            predictions[target] = self._predict_single_target(target, tokenizer, model, num_generate, num_optim_tokens,
                                                              num_steps, verbose)
        return predictions

    def _predict_single_target(self, target, tokenizer, model, num_generate, num_optim_tokens,
                               num_steps, verbose):
        """
        Generate predicted trigger for the provided target
        """
        self.add_hooks(model)
        target_tokens = make_target_batch(tokenizer,[target])
        embedding_weight = get_embedding_weight(model) # save the word embedding matrix
        optim_prompts = []
        for _ in range(num_generate):
            if verbose:
                print(f"Generating trigger {_+1}/{num_generate}")
            #
            total_vocab_size = model.gpt_neox.embed_in.weight.shape[0]  
            trigger_token_length = num_optim_tokens 
            batch_size_loss = target_tokens.shape[0]
            trigger_tokens = np.random.randint(total_vocab_size, size=trigger_token_length)

            model.zero_grad()
            loss = get_loss(model, batch_size_loss, trigger_tokens, target_tokens)
            best_loss = loss
            counter = 0
            end_iter = False

            for i in range(num_steps): 
                for token_to_flip in range(0, trigger_token_length): 
                    if end_iter:  
                        continue

                    extracted_grads = [] 
                    self.extracted_grads = []
                    loss.backward()
                    averaged_grad = torch.sum(self.extracted_grads[0], dim=0)
                    averaged_grad = averaged_grad[token_to_flip].unsqueeze(0)
                    candidates = hotflip_attack(averaged_grad, embedding_weight,
                                                        [trigger_tokens[token_to_flip]], 
                                                        increase_loss=False, num_candidates=3)[0]

                    curr_best_loss = 999999
                    curr_best_trigger_tokens = None
                    for cand in candidates:
                        candidate_trigger_tokens = deepcopy(trigger_tokens)
                        candidate_trigger_tokens[token_to_flip] = cand
                        curr_loss = get_loss(model, batch_size_loss, candidate_trigger_tokens,
                                            target_tokens)
                        if curr_loss < curr_best_loss:
                            curr_best_loss = curr_loss
                            curr_best_trigger_tokens = deepcopy(candidate_trigger_tokens)

                    if curr_best_loss < best_loss:
                        counter = 0 
                        best_loss = curr_best_loss
                        trigger_tokens = deepcopy(curr_best_trigger_tokens)

                    elif counter == len(trigger_tokens):
                        end_iter = True
                    else:
                        counter = counter + 1

                    model.zero_grad()
                    loss = get_loss(model, batch_size_loss, trigger_tokens, target_tokens)
                if verbose:
                    if i % 10 == 0:
                        print('{} {:.3f}'.format(i, best_loss.data.item()))
            optim_prompts.append(tokenizer.decode(trigger_tokens))
            if verbose:
                print('{} {:.3f}'.format(i, best_loss.data.item()))

        # ========== detokenize and print the optimized prompt ========== #
        if verbose:
            print('target_text:', target)
            for i, p in enumerate(optim_prompts):
                print(f'optim_prompt {i}:', p)
        
        return optim_prompts