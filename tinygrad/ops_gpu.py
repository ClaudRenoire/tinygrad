import numpy as np
from .tensor import Function, register, GPUBuffer, Tensor, Device, OPS_NAMES
import pyopencl as cl
import functools

def buffer_new(ctx, shape, zero=False):
  return GPUBuffer(shape, hostbuf=None if not zero else np.zeros(shape, dtype=np.float32))

@functools.lru_cache()
def clbuild(cl_ctx, name, prg):
  return cl.Program(cl_ctx, prg).build().__getattr__(name)

def uint2(x, y):
  return np.array((x,y), dtype=cl.cltypes.uint2)
i32 = np.int32

def subsample_op(ctx, input, kernel_size, stride, iter_op, result_op, decls=''):
  py, px = stride
  N, C, Yin, Xin = input.shape
  Yout, Xout = (Yin-kernel_size[0])//py+1, (Xin-kernel_size[1])//px+1
  ret = buffer_new(ctx, (N, C, Yout, Xout), zero=True)
  subsample = clbuild(ctx.cl_ctx, "subsample", """
  __kernel void subsample(__global float *output, __global const float *input, uint2 osize, uint2 isize,
                          uint2 ksz, uint2 stride) {
    int3 gid = (int3)(get_global_id(2), get_global_id(1), get_global_id(0));
    int oid = gid.x + osize.x*(gid.y + osize.y*gid.z);
    """+decls+""";
    for (uint j=0; j<ksz.y; ++j) {
      for (uint i=0; i<ksz.x; ++i) {
        int iid = (gid.x*stride.x+i) + isize.x*((gid.y*stride.y+j) + isize.y*gid.z);
        if (gid.x*stride.x+i < isize.x && gid.y*stride.y+j < isize.y) {
          """+iter_op+""";
        }
      }
    }
    output[oid] = """+result_op+""";
  }""")
  subsample(ctx.cl_queue, (N*C, Yout, Xout), None,
            ret.cl, input.cl, uint2(Xout, Yout), uint2(Xin, Yin),
            uint2(*kernel_size[::-1]), uint2(px, py))
  ctx.data = np.empty((N, C, Yout, Xout)) # set shape expectation on tensor instance
  return ret

def supersample_op(ctx, input, out_shape, kernel_size, result_op, decls='', input2=None):
  (N, C, Yin, Xin), (Yout, Xout) = input.shape, out_shape[2:]
  py,px = kernel_size
  ret = buffer_new(ctx, out_shape, zero=True)
  supsample = clbuild(ctx.cl_ctx, "supsample", """
  __kernel void supsample(__global float *output, __global const float *input, __global const void *input2,
                          uint2 osize, uint2 isize, uint2 ksz) {
    int3 gid = (int3)(get_global_id(2), get_global_id(1), get_global_id(0));
    int oid = gid.x + osize.x*(gid.y + osize.y*gid.z);
    int iid = (gid.x/ksz.x) + isize.x*((gid.y/ksz.y) + isize.y*gid.z);
    """+decls+""";
    if (gid.x/ksz.x < isize.x && gid.y/ksz.y < isize.y) {
      output[oid] = """+result_op+""";
    }
  }""")
  supsample(ctx.cl_queue, (N*C, Yout, Xout), None,
            ret.cl, input.cl, input2.cl if input2 is not None else input2,
            uint2(Xout, Yout), uint2(Xin, Yin), uint2(px, py))
  ctx.data = np.empty((N, C, Yout, Xout)) # set shape expectation on tensor instance
  return ret

@functools.lru_cache()
def get_binop_prg(cl_ctx, code, complist):
  ndims = len(complist)
  args = "".join([", int d%d" % i for i in range(ndims)]) + "".join([", int p%d" % i for i in range(ndims-1)])
  compute_idx_rets = ["\n    int idx_ret"+str(i)+" = (gid0 / "+("p%d"%i if i < ndims-1 else "1")+") % d"+str(i)+";" for i in range(ndims)]

  idx_exprs = ["0", "0"] # [idx_x, idx_y]
  for i in range(ndims):
    for j in range(2):
      if complist[i][j]:
        idx_exprs[j] = "idx_ret%d + d%d*(%s)" % (i, i, idx_exprs[j])

  return cl.Program(cl_ctx, """__kernel void binop(__global const float *x_g, __global const float *y_g, __global float *res_g"""+args+""") {
    int gid0 = get_global_id(0);"""+"".join(compute_idx_rets)+"""
    float a = x_g["""+idx_exprs[0]+"""];
    float b = y_g["""+idx_exprs[1]+"""];
    res_g[gid0] = """+code+""";\n}""").build()

def binary_op(ctx, code, x, y):
  n_dims = max(len(x.shape), len(y.shape))
  shape_x, shape_y = np.ones(n_dims, dtype=np.int32), np.ones(n_dims, dtype=np.int32)
  shape_x[:len(x.shape)] = np.array(x.shape, dtype=np.int32)
  shape_y[:len(y.shape)] = np.array(y.shape, dtype=np.int32)
  if not np.all((shape_x == 1) | (shape_y == 1) | (shape_x == shape_y)):
    raise Exception(f"binary op unbroadcastable shape mismatch: {x.shape} vs {y.shape}")
  shape_ret = np.maximum(shape_x, shape_y)

  dimlist, complist = [], [] # note: len(dimlist) may be less than n_dims
  def push(dim, comp):
    if len(complist) > 0 and complist[-1] == comp:
      dimlist[-1] *= dim
    elif comp != (False, False):
      dimlist.append(dim); complist.append(comp)
  for i in range(n_dims): # group together any adjacent dimensions that we can to simplify broadcasting
    push(i32(max(shape_x[i], shape_y[i])), (shape_x[i] > 1, shape_y[i] > 1))

  prg = get_binop_prg(ctx.cl_ctx, code, tuple(complist))
  ret = buffer_new(ctx, shape_ret, zero=True)
  prod_list = np.array(dimlist, dtype=i32)[-1::-1].cumprod(dtype=i32)[-1::-1] # take cumprod from back to front
  prg.binop(ctx.cl_queue, [prod_list[0]] if len(dimlist) > 0 else [1], None, x.cl, y.cl, ret.cl, *dimlist, *(prod_list[1:]))
  return ret

def unary_op(ctx, code, x):
  ret = buffer_new(ctx, x.shape)
  unop = clbuild(ctx.cl_ctx, "unop", """
  __kernel void unop(__global const float *a_g, __global float *res_g) {
    int gid = get_global_id(0);
    float a = a_g[gid];
    res_g[gid] = """+code+""";
  }""")
  unop(ctx.cl_queue, [np.prod(ret.shape)], None, x.cl, ret.cl)
  return ret

def reduce_op(ctx, code, code2, inp, axis=None):
  if axis is None:
    # full reduce
    osize = [1]*len(inp.shape)
  else:
    osize = np.array(inp.shape)
    osize[list(axis)] = 1
  ret = buffer_new(ctx, osize)
  if axis is None:
    ret.shape = (1,)

  # TODO: this is insanely slow
  reduce = clbuild(ctx.cl_ctx, "reduce", """
  __kernel void reduce(__global const float *a_g, int sz, __global float *res_g, int prod, int n_dims,
                       __global const int *shape_x, __global const int *shape_ret) {
    int gid = get_global_id(0);

    float out = 0.0;
    for (int x = 0; x < sz; x++) {
      int idx = 0;  // compute index into a_g
      int tprod = prod;
      int tsz = sz;
      for (int dim = 0; dim < n_dims; dim++) {
        idx *= shape_x[dim];
        if (shape_x[dim] == shape_ret[dim]) {   // dim from gid, don't reduce
          tprod /= shape_x[dim];
          idx += (gid / tprod) % shape_x[dim];
        } else {  // dim from x
          tsz /= shape_x[dim];
          idx += (x / tsz) % shape_x[dim];
        }
      }
      float a = a_g[idx];
      """+code+""";
    }
    res_g[gid] = """+code2+""";
  }""")
  buffer_np = lambda x: cl.Buffer(ctx.cl_ctx,
    cl.mem_flags.READ_WRITE | cl.mem_flags.COPY_HOST_PTR, hostbuf=x)
  reduce(ctx.cl_queue, [np.prod(osize)], None, inp.cl,
    i32(np.prod(inp.shape)//np.prod(osize)), ret.cl,
    i32(np.prod(osize)), i32(len(osize)),
    buffer_np(np.array(inp.shape, dtype=np.int32)),
    buffer_np(np.array(osize, dtype=np.int32)))
  return ret

def unbroadcast(ctx, out, in_sh):
  sum_axis = [i for i in range(len(in_sh)) if in_sh[i]==1 and out.shape[i]>1] if in_sh != (1,) else None
  return reduce_op(ctx, "out += a", "out", out, sum_axis)

# ***** now for the ops themselves *****

class Add(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x.shape, y.shape)
    return binary_op(ctx, 'a+b', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    grad_x, grad_y = grad_output, grad_output
    shape_x, shape_y = ctx.saved_tensors
    return unbroadcast(ctx, grad_x, shape_x), unbroadcast(ctx, grad_y, shape_y),
register('add', Add, device=Device.GPU)

class Sub(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x.shape, y.shape)
    return binary_op(ctx, 'a-b', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    grad_x, grad_y = grad_output, unary_op(ctx, '-a', grad_output)
    shape_x, shape_y = ctx.saved_tensors
    return unbroadcast(ctx, grad_x, shape_x), unbroadcast(ctx, grad_y, shape_y),

class Mul(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return binary_op(ctx, 'a*b', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    grad_x = binary_op(ctx, 'a*b', y, grad_output)
    grad_y = binary_op(ctx, 'a*b', x, grad_output)
    return unbroadcast(ctx, grad_x, x.shape), unbroadcast(ctx, grad_y, y.shape),

class Pow(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return binary_op(ctx, 'pow(a,b)', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    grad_x = binary_op(ctx, 'a*b', grad_output,
                      binary_op(ctx, 'b * (pow((float)a, (float)(b-1.0)))', x, y))
    grad_y = binary_op(ctx, 'a*b', grad_output,
                      binary_op(ctx, 'pow(a, (float)b) * log(a);', x, y))
    return unbroadcast(ctx, grad_x, x.shape), unbroadcast(ctx, grad_y, y.shape),

class Sum(Function):
  @staticmethod
  def forward(ctx, input, axis=None):
    ctx.save_for_backward(input, axis)
    ret = reduce_op(ctx, "out += a", "out", input, axis=axis)
    if axis is not None:
      ret.shape = tuple([input.shape[i] for i in range(len(input.shape)) if i not in axis])
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    input, axis = ctx.saved_tensors
    shape = [1 if axis is None or i in axis else input.shape[i] for i in range(len(input.shape))]
    output = GPUBuffer(shape, hostbuf=grad_output)
    return binary_op(ctx, 'a+b', output, buffer_new(ctx, input.shape, zero=True))

class Dot(Function):
  @staticmethod
  def forward(ctx, input, weight):
    assert input.shape[1] == weight.shape[0]
    isize, msize, osize = i32(input.shape[0]), i32(input.shape[1]), i32(weight.shape[1])
    ret = buffer_new(ctx, (isize, osize))

    matmul = clbuild(ctx.cl_ctx, "matmul", """
    __kernel void matmul(
      __global const float *input, __global const float *weight, __global float *res,
      int is0, int is1, int msize, int ws0, int ws1, int osize
   ) {
      int X = get_global_id(0); // isize
      int Y = get_global_id(1); // osize

      float ret = 0.0;
      for (int x = 0; x < msize; x++) {
        ret += input[X * is0 + x * is1] * weight[Y * ws0 + x * ws1];
      }

      res[X * osize + Y] = ret;
    }""")
    ctx.save_for_backward(input, weight, matmul)

    # (isize,msize) x (msize,osize) = (isize,osize)
    matmul(ctx.cl_queue, [isize, osize], None,
      input.cl, weight.cl, ret.cl,
      msize, i32(1), msize, i32(1), osize, osize)
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    input, weight, matmul = ctx.saved_tensors
    isize, msize, osize = i32(input.shape[0]), i32(input.shape[1]), i32(weight.shape[1])

    grad_input = buffer_new(ctx, input.shape)
    grad_weight = buffer_new(ctx, weight.shape)

    # (isize,osize) x (msize,osize) = (isize,msize)
    matmul(ctx.cl_queue, [isize, msize], None,
      grad_output.cl, weight.cl, grad_input.cl,
      osize, i32(1), osize, osize, i32(1), msize)

    # (isize,msize) x (isize,osize) = (msize,osize)
    matmul(ctx.cl_queue, [msize, osize], None,
      input.cl, grad_output.cl, grad_weight.cl,
      i32(1), msize, isize, i32(1), osize, osize)

    return grad_input, grad_weight

# ************* simple ops *************

class Pad2D(Function):
  @staticmethod
  def forward(ctx, x, padding=None):
    bs,cin,iy,ix = x.shape
    oy,ox = iy+padding[2]+padding[3], ix+padding[0]+padding[1]
    ret = buffer_new(ctx, (bs, cin, oy, ox), zero=True)

    pad2d = clbuild(ctx.cl_ctx, "pad2d", """
    __kernel void pad2d(__global const float *input, __global float *output,
                        int ipx, int ipy, int py, int px, int oy, int ox, int iy, int ix) {
      int BC = get_global_id(0);
      int Y = get_global_id(1);
      int X = get_global_id(2);

      int iptr = BC*iy*ix + (Y+ipy)*ix + ipx + X;
      int optr = BC*oy*ox + (Y+py)*ox + px + X;

      output[optr] = input[iptr];
    }""")
    ctx.save_for_backward(padding, pad2d)
    pad2d(ctx.cl_queue, [bs*cin, iy, ix], None,
        x.cl, ret.cl,
        i32(0), i32(0), i32(padding[2]), i32(padding[0]),
        i32(oy), i32(ox), i32(iy), i32(ix)
      )
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    padding, pad2d = ctx.saved_tensors
    bs, cin, iy, ix = grad_output.shape
    oy, ox = iy - padding[2] - padding[3], ix - padding[0] - padding[1]
    ret = buffer_new(ctx, (bs, cin, oy, ox))
    pad2d(ctx.cl_queue, [bs*cin, oy, ox], None,
              grad_output.cl, ret.cl,
              i32(padding[2]), i32(padding[0]), i32(0), i32(0),
              i32(oy), i32(ox), i32(iy), i32(ix)
             )
    return ret

class Reshape(Function):
  @staticmethod
  def forward(ctx, x, shape):
    ctx.save_for_backward(x.shape)
    shape = tuple(-np.prod(x.shape) // np.prod(shape) if s == -1 else s for s in shape)
    r = GPUBuffer(shape, hostbuf=x)
    assert np.prod(x.shape) == np.prod(r.shape)
    return r

  @staticmethod
  def backward(ctx, grad_output):
    in_shape, = ctx.saved_tensors
    return GPUBuffer(in_shape, hostbuf=grad_output)

# ************* activation ops *************

class ReLU(Function):
  @staticmethod
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return unary_op(ctx, 'max(a, (float)0.)', input)

  @staticmethod
  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return binary_op(ctx, 'a * (b >= 0)', grad_output, input)

class Sigmoid(Function):
  @staticmethod
  def forward(ctx, input):
    ret = unary_op(ctx, '1./(1+exp(-a))', input)
    ctx.save_for_backward(ret)
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    ret, = ctx.saved_tensors
    return binary_op(ctx, 'a * (b * (1 - b));', grad_output, ret)

class AvgPool2D(Function):
  @staticmethod
  def forward(ctx, input, kernel_size=(2, 2)):
    ret = subsample_op(ctx, input, kernel_size, kernel_size, iter_op="sumval += input[iid]",
      result_op="sumval / (ksz.x * ksz.y)", decls="float sumval=0.f")
    ctx.save_for_backward(input.shape)
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    orig_shape, = ctx.saved_tensors
    return supersample_op(ctx, grad_output, orig_shape, ctx.kernel_size,
      result_op="input[iid] / (ksz.x * ksz.y)")

class MaxPool2D(Function):
  @staticmethod
  def forward(ctx, input, kernel_size=(2, 2)):
    idxs = subsample_op(ctx, input, kernel_size, kernel_size,
      iter_op="if (input[iid]>maxval) { maxval = input[iid]; maxidx = j * ksz.x + i; }",
      result_op="(float)maxidx", decls="float maxval=-FLT_MAX; int maxidx=0")
    ctx.save_for_backward(idxs, input.shape)
    return subsample_op(ctx, input, kernel_size, kernel_size,
      iter_op="maxval = max(maxval, input[iid])",
      result_op="maxval", decls="float maxval = -FLT_MAX")

  @staticmethod
  def backward(ctx, grad_output):
    idxs, orig_shape = ctx.saved_tensors
    return supersample_op(ctx, grad_output, orig_shape, ctx.kernel_size,
      result_op="(maxidx == kernidx) * input[iid]",
      decls="int maxidx=((__global float*)input2)[iid]; int kernidx=(gid.x%ksz.x) + ksz.x*(gid.y%ksz.y)",
      input2=idxs)

class LogSoftmax(Function):
  @staticmethod
  def forward(ctx, input):
    # TODO: stability?
    lsum = reduce_op(ctx, "out += exp(a)", "log(out)", input, axis=[1])
    output = binary_op(ctx, 'a-b', input, lsum)
    ctx.save_for_backward(output)
    return output

  @staticmethod
  def backward(ctx, grad_output):
    output, = ctx.saved_tensors
    lsum = reduce_op(ctx, "out += a", "out", grad_output, axis=[1])
    texp = binary_op(ctx, "exp(a) * b", output, lsum)
    return binary_op(ctx, "a - b", grad_output, texp)

# ************* conv ops *************

class Conv2D(Function):
  @staticmethod
  def forward(ctx, x, w, stride=1, groups=1):
    if type(ctx.stride) == int:
      ctx.stride = (ctx.stride, ctx.stride)
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_,iy,ix = x.shape
    oy,ox = (iy-(H-ys))//ys, (ix-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    ctx.save_for_backward(x,w)

    # output buffer
    ret = buffer_new(ctx, (bs, cout, oy, ox))

    # input  = (bs, groups, cin, iy, ix)
    # weight = (groups, rcout, cin, H, W)
    # output = (bs, groups, rcout, oy, ox)

    conv = clbuild(ctx.cl_ctx, "conv", """
    __kernel void conv(__global const float *input, __global const float *weight, __global float *output,
      int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs) {

      int B = get_global_id(0)/(groups*rcout);  // range 0-bs
      int g = (get_global_id(0)/rcout)%groups;
      int c = get_global_id(0) % rcout;

      int Y = get_global_id(1);  // range 0-oy
      int X = get_global_id(2);  // range 0-ox
      int IY = Y*ys;
      int IX = X*xs;

      float acc = 0.0;
      for (int ci = 0; ci < cin; ci++) {
        for (int y = IY; y < IY+H; y++) {
          for (int x = IX; x < IX+W; x++) {
            acc += input[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + y*ix + x] * \
              weight[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + (y-IY)*W + (x-IX)];
          }
        }
      }
      output[B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X] = acc;
    }""")

    conv(ctx.cl_queue, [bs*groups*rcout, oy, ox], None,
      x.cl, w.cl, ret.cl,
      i32(H), i32(W), i32(groups), i32(rcout), i32(cin),
      i32(oy), i32(ox), i32(iy), i32(ix), i32(ys), i32(xs)
    )
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    bs,_,oy,ox = grad_output.shape
    x, w = ctx.saved_tensors
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_,iy,ix = x.shape
    oy,ox = (iy-(H-ys))//ys, (ix-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    dx = buffer_new(ctx, (bs, cin_, iy, ix), zero=True)
    dw = buffer_new(ctx, (cout, cin, H, W))

    # tensx = (bs, groups*cin, iy, ix)
    # tensw = (groups*rcout, cin, H, W)
    # ggg = (bs, groups*rout, oy, ox)

    convw = clbuild(ctx.cl_ctx, "convw", """
    __kernel void convw(__global const float *tensx, __global const float *ggg, __global float *dw,
      int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

      int g = get_global_id(0)/(rcout*cin) ; // range 0-groups
      int c = (get_global_id(0)/(cin)) %rcout; // range 0-rcout
      int ci = get_global_id(0) % cin;        // range 0-cin
      int y = get_global_id(1);  // range 0-H
      int x = get_global_id(2);  // range 0-W

      float acc = 0.0;
      for (int Y = 0; Y < oy; Y++) {
        for (int X = 0; X < ox; X++) {
          for (int B = 0; B < bs; B++) {
            acc += ggg[B*groups*rcout*oy*ox + +g*rcout*oy*ox + c*oy*ox + Y*ox + X] * \
              tensx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + (Y*ys+y)*ix + X*xs+x];
          }
        }
      }
      dw[get_global_id(0)*H*W + y*W + x] = acc;
    }""")
    convx = clbuild(ctx.cl_ctx, "convx", """
    __kernel void convx(__global const float *tensw, __global const float *ggg, __global float *dx,
      int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

      int B = get_global_id(0);
      int g = get_global_id(1);
      int ci = get_global_id(2);

      for (int Y = 0; Y < oy; Y++) {
        for (int X = 0; X < ox; X++) {
          for (int y = 0; y < H; y++) {
            for (int x = 0; x < W; x++) {
              float acc = 0.0;
              for (int c = 0; c < rcout; c++) {
                acc += ggg[B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X] * \
                  tensw[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
              }
              dx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + (Y*ys+y)*ix + X*xs+x] += acc;
            }
          }
        }
      }
    }
    """)

    conv_args = i32(H), i32(W), i32(ctx.groups), i32(rcout), i32(cin), i32(oy), i32(ox), i32(iy), i32(ix), i32(ys), i32(xs), i32(bs)
    convw(ctx.cl_queue, [ctx.groups*rcout*cin, H, W], None, x.cl, grad_output.cl, dw.cl, *conv_args)
    convx(ctx.cl_queue, [bs, ctx.groups, cin], None, w.cl, grad_output.cl, dx.cl, *conv_args)
    return dx, dw
register(OPS_NAMES, [Add, Sub, Mul, Pow, Sum, Dot, Pad2D, Reshape, ReLu, Sigmoid, LogSoftmax, Conv2D, MaxPool2D, AvgPool2D], device=Device.GPU))
