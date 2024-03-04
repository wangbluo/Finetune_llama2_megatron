import torch
import torch.distributed as dist
from torch.testing._internal.common_utils import TestCase
from transformers import logging
from typing import Any, Dict, List, Optional, Tuple, Union
from transformers.cache_utils import Cache

logger = logging.get_logger(__name__)

device = "cuda" if torch.cuda.is_available() else "cpu" 
tp_size = 1

def set_tp_size(tp):
    tp_size = tp
    
def _check_module(m1, m2, check_grad=False):
        testcase = TestCase()
        named_parameters = dict(m1.named_parameters())
        for name, param_m2 in m2.named_parameters():
            testcase.assertTrue(name in named_parameters)
            param_m1 = named_parameters[name]
            if check_grad:
                param_m2 = param_m2.grad
                param_m1 = param_m1.grad
            testcase.assertEqual(param_m2, param_m1)

def _check_module_bwd(m1, m2, check_grad=False):
    testcase = TestCase()
    named_parameters = dict(m1.named_parameters())
    for name, param_m2 in m2.named_parameters():
        testcase.assertTrue(name in named_parameters)
        param_m1 = named_parameters[name]
        if check_grad:
            param_m2 = param_m2.grad
            param_m1 = param_m1.grad
            if "q_proj" in name or "k_proj" in name or "v_proj" in name or "gate_proj" in name or "up_proj" in name:
                # cancat the splited param_m2.grad
                process_group = dist.group.WORLD
                tensor_list = [torch.empty_like(param_m2) for _ in range(dist.get_world_size())]
                rank = dist.get_rank()
                tensor_list[rank] = param_m2     
                dist.all_gather(tensor_list, param_m2, group=process_group)
                # Note: torch.cat already creates a contiguous tensor.
                first_dim = param_m2.shape[0]
                param_m2 = torch.cat(tensor_list, dim = 0).contiguous()
            if "o_proj" in name or "down_proj" in name:
                # cancat the splited param_m2.grad
                process_group = dist.group.WORLD
                tensor_list = [torch.empty_like(param_m2) for _ in range(dist.get_world_size())]
                rank = dist.get_rank()
                tensor_list[rank] = param_m2     
                dist.all_gather(tensor_list, param_m2, group=process_group)
                # Note: torch.cat already creates a contiguous tensor.
                first_dim = param_m2.shape[0]
                param_m2 = torch.cat(tensor_list, dim = -1).contiguous()
                
        testcase.assertEqual(param_m2, param_m1)
 
#before q, k, v_proj
class InputTpLinear(torch.autograd.Function):
    """Pass the input to the model parallel region."""

    @staticmethod
    def symbolic(graph, input_):
        return input_

    @staticmethod
    def forward(ctx, input_):
        return input_

    @staticmethod
    def backward(ctx, grad_output):
        process_group = dist.group.WORLD
        if not grad_output.is_contiguous():
            grad_output = grad_output.contiguous()
        dist.all_reduce(grad_output, op=dist.ReduceOp.SUM, group=process_group)
        return grad_output 

def input_tp_linear(input):
    return InputTpLinear.apply(input)

# for the q k v_proj output
class ColTpLinear(torch.autograd.Function):
    @staticmethod
    def forward(ctx, output):
        #split qkv_weight have already done in get_tensor_sharded_model() 
        
        return output
        
    @staticmethod
    def backward(ctx, grad):
        #all_gather the grad, [384, 768] -> [768, 768]
        print(grad)
        print(grad.shape)
        process_group = dist.group.WORLD
        tensor_list = [torch.empty_like(grad) for _ in range(dist.get_world_size())]
        rank = dist.get_rank()
        tensor_list[rank] = grad     
        dist.all_gather(tensor_list, grad, group=process_group)
        # Note: torch.cat already creates a contiguous tensor.
        first_dim = grad.shape[0]
        grad = torch.cat(tensor_list, dim=first_dim).contiguous()
        return grad

def col_tp_linear(output):
    return ColTpLinear.apply(output)  
  
# for the o_proj output
class RowTpLinear(torch.autograd.Function):
    @staticmethod
    def forward(ctx, output):
        # Use c10d collectselfops
        process_group = dist.group.WORLD
        if not output[0].is_contiguous():
            output[0] = output[0].contiguous()
        dist.all_reduce(output[0], op=dist.ReduceOp.SUM, group=process_group)
        return output[0]
        
    @staticmethod
    def backward(ctx, grad):
        bs = 1
        grad = grad.view(bs, 2048, 2048)
        return grad
    
def row_tp_linear(output):
    return RowTpLinear.apply(output)
        
def bind(instance, func, as_name=None):
    """
    Bind the function *func* to *instance*, with either provided name *as_name*
    or the existing name of *func*. The provided *func* should accept the 
    instance as the first argument, i.e. "self".
    """
    if as_name is None:
        as_name = func.__name__
    bound_method = func.__get__(instance, instance.__class__)
    setattr(instance, as_name, bound_method)
    return bound_method
    
def get_tensor_sharded_model(model, tp_size):

    # Parallelize the Attention and MLP submodules.
    for layer in model.model.layers:
        # nn.linear will transpose the weight, so we should do row split for qkv and do col split for o.
        # For example, we should make q shape from [2048, 2048] -> [1024, 2048]
        set_tp_size(tp_size)
        
        layer.self_attn.tp = tp_size
        
        q_proj = layer.self_attn.q_proj.weight
        k_proj = layer.self_attn.k_proj.weight 
        v_proj = layer.self_attn.v_proj.weight 
        o_proj = layer.self_attn.o_proj.weight  
        row_split_dim = q_proj.shape[0] // tp_size
        col_split_dim = o_proj.shape[-1] // tp_size
        
        split_q_tensors = torch.split(q_proj, row_split_dim, dim = 0)
        split_k_tensors = torch.split(k_proj, row_split_dim, dim = 0)
        split_v_tensors = torch.split(v_proj, row_split_dim, dim = 0)
        split_o_tensors = torch.split(o_proj, col_split_dim, dim = -1)
        # tp_size is no need larger than device_count
        for i in range(len(split_q_tensors)):
            if dist.get_rank()==i:
                layer.self_attn.q_proj.weight.data = split_q_tensors[i].to(f"{device}:{i}")
                layer.self_attn.k_proj.weight.data = split_k_tensors[i].to(f"{device}:{i}")
                layer.self_attn.v_proj.weight.data = split_v_tensors[i].to(f"{device}:{i}")
                layer.self_attn.o_proj.weight.data = split_o_tensors[i].to(f"{device}:{i}")
   
        bind(layer.self_attn, forward, "forward")

        # Manually adjust the number of heads after sharding the self attention modules.
        # For Llama2 models, your should adjust the number of heads separately.
        assert model.model.config.num_attention_heads % dist.get_world_size() == 0
        layer.self_attn.num_heads = model.model.config.num_attention_heads // dist.get_world_size()
        layer.self_attn.num_key_value_heads = model.model.config.num_key_value_heads // dist.get_world_size()
        layer.self_attn.hidden_size = model.model.config.hidden_size // dist.get_world_size()
        
        # shard the MLP part
        layer.mlp.tp= tp_size
        gate_proj = layer.mlp.gate_proj.weight
        up_proj = layer.mlp.up_proj.weight
        down_proj = layer.mlp.down_proj.weight
        row_split_dim =  gate_proj.shape[0] // tp_size
        col_split_dim = down_proj.shape[-1] // tp_size
        split_gate_tensors = torch.split(gate_proj, row_split_dim, dim = 0)
        split_up_tensors = torch.split(up_proj, row_split_dim, dim = 0)
        split_down_tensors = torch.split(down_proj, col_split_dim, dim = -1)
        for i in range(len(split_gate_tensors)):
            if dist.get_rank()==i:
                layer.mlp.gate_proj.weight.data = split_gate_tensors[i].to(f"{device}:{i}")
                layer.mlp.up_proj.weight.data = split_up_tensors[i].to(f"{device}:{i}")
                layer.mlp.down_proj.weight.data = split_down_tensors[i].to(f"{device}:{i}")
   
        bind(layer.mlp, mlp_forward, "forward") 
    
# bind the new forward function with llama2 model's self_attn
def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)

def apply_rotary_pos_emb(q, k, cos, sin, position_ids=None, unsqueeze_dim=1):

    cos = cos.unsqueeze(unsqueeze_dim)
    sin = sin.unsqueeze(unsqueeze_dim)
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed

def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)
    
def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        cache_position: Optional[torch.LongTensor] = None,
) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:

        bsz, q_len, _ = hidden_states.size()
        if hasattr(self, 'tp') and self.tp > 1:
            hidden_states = input_tp_linear(hidden_states)

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        cos, sin = self.rotary_emb(value_states, position_ids)
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin)

        # In case static cache is used, it is an instance attribute.
        past_key_value = getattr(self, "past_key_value", past_key_value)

        if past_key_value is not None:
            # sin and cos are specific to RoPE models; position_ids needed for the static cache
            cache_kwargs = {"sin": sin, "cos": cos, "cache_position": cache_position}
            key_states, value_states = past_key_value.update(key_states, value_states, self.layer_idx, cache_kwargs)

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        causal_mask = attention_mask
        if attention_mask is not None and cache_position is not None:
            causal_mask = causal_mask[:, :, cache_position, : key_states.shape[-2]]

        # SDPA with memory-efficient backend is currently (torch==2.1.2) bugged with non-contiguous inputs with custom attn_mask,
        # Reference: https://github.com/pytorch/pytorch/issues/112577.
        if query_states.device.type == "cuda" and causal_mask is not None:
            query_states = query_states.contiguous()
            key_states = key_states.contiguous()
            value_states = value_states.contiguous()

        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            attn_mask=causal_mask,
            dropout_p=self.attention_dropout if self.training else 0.0,
        )

        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.view(bsz, q_len, self.hidden_size)

        attn_output = self.o_proj(attn_output)
        
        if hasattr(self, 'tp') and self.tp > 1:
            attn_output = row_tp_linear(attn_output)

        return attn_output, None, past_key_value 
      
        
# bind the new forward function with llama2 model's self_attn
import torch.nn.functional as F
def mlp_forward(self, x):
    if self.config.pretraining_tp > 1:
        slice = self.intermediate_size // self.config.pretraining_tp
        gate_proj_slices = self.gate_proj.weight.split(slice, dim=0)
        up_proj_slices = self.up_proj.weight.split(slice, dim=0)
        down_proj_slices = self.down_proj.weight.split(slice, dim=1)

        gate_proj = torch.cat(
            [F.linear(x, gate_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1
        )
        up_proj = torch.cat([F.linear(x, up_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1)

        intermediate_states = (self.act_fn(gate_proj) * up_proj).split(slice, dim=2)
        down_proj = [
            F.linear(intermediate_states[i], down_proj_slices[i]) for i in range(self.config.pretraining_tp)
        ]
        down_proj = sum(down_proj)
    else:
        if hasattr(self, 'tp') and self.tp > 1:
            x = input_tp_linear(x)
        down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))
        if hasattr(self, 'tp') and self.tp > 1:
            down_proj = row_tp_linear(down_proj)   

    return down_proj