import re
import sys
import ast
import inspect
import torch
from .._jit_internal import List, BroadcastingList1, BroadcastingList2, \
    BroadcastingList3, Tuple, is_tuple, is_list, Dict, is_dict
from torch._C import TensorType, TupleType, FloatType, IntType, \
    ListType, StringType, DictType
from textwrap import dedent


PY35 = sys.version_info >= (3, 5)


class Module(object):
    def __init__(self, name, members):
        self.name = name
        self.members = members

    def __getattr__(self, name):
        try:
            return self.members[name]
        except KeyError:
            raise RuntimeError("Module {} has no member called {}".format(self.name, name))


_eval_env = {
    'torch': Module('torch', {'Tensor': torch.Tensor}),
    'Tensor': torch.Tensor,
    'typing': Module('typing', {'Tuple': Tuple}),
    'Tuple': Tuple,
    'List': List,
    'Dict': Dict,
}


def get_signature(fn):
    # Python 3.5 adds support for the nice annotation syntax, so try that first.
    if PY35:
        sig = try_real_annotations(fn)
        if sig is not None:
            return sig

    type_line, source = None, None
    try:
        source = dedent(inspect.getsource(fn))
        type_line = get_type_line(source)
    except TypeError:
        pass
    # This might happen both because we failed to get the source of fn, or
    # because it didn't have any annotations.
    if type_line is None:
        return None

    return parse_type_line(type_line)


# This is essentially a weaker form of get_signature(), where we don't care if
# we have the types, we just care that we can figure out how many parameters
# a function takes.
def get_num_params(fn):
    try:
        source = dedent(inspect.getsource(fn))
    except (TypeError, IOError):
        return None
    if source is None:
        return None
    py_ast = ast.parse(source)
    if len(py_ast.body) == 1 and isinstance(py_ast.body[0], ast.ClassDef):
        raise RuntimeError("cannot instantiate class object ({}) inside jit.script".format(py_ast.body[0].name))
    if len(py_ast.body) != 1 or not isinstance(py_ast.body[0], ast.FunctionDef):
        raise RuntimeError("expected a single top-level function")
    py_def = py_ast.body[0]
    if py_def.args.vararg is not None:
        return None
    elif hasattr(py_def.args, 'kwonlyargs') and len(py_def.args.kwonlyargs) > 0:
        return None
    else:
        num_params = len(py_def.args.args)
        if inspect.ismethod(fn):
            num_params = num_params - 1
        return num_params


def parse_type_line(type_line):
    """Parses a type annotation specified as a comment.

    Example inputs:
        # type: (Tensor, torch.Tensor) -> Tuple[Tensor]
        # type: (Tensor, Tuple[Tensor, Tensor]) -> Tensor
    """
    arg_ann_str, ret_ann_str = split_type_line(type_line)

    try:
        arg_ann = eval(arg_ann_str, _eval_env)
    except (NameError, SyntaxError) as e:
        raise RuntimeError("Failed to parse the argument list of a type annotation: {}".format(str(e)))

    if not isinstance(arg_ann, tuple):
        arg_ann = (arg_ann,)

    try:
        ret_ann = eval(ret_ann_str, _eval_env)
    except (NameError, SyntaxError) as e:
        raise RuntimeError("Failed to parse the return type of a type annotation: {}".format(str(e)))

    arg_types = [ann_to_type(ann) for ann in arg_ann]
    return arg_types, ann_to_type(ret_ann)


def get_type_line(source):
    """Tries to find the line containing a comment with the type annotation."""
    type_comment = '# type:'

    lines = source.split('\n')
    lines = [(line_num, line) for line_num, line in enumerate(lines)]
    type_lines = list(filter(lambda line: type_comment in line[1], lines))

    if len(lines) == 0:
        return None
    elif len(lines) == 1:
        # Only 1 type line, quit now
        return lines[0][1].strip()

    # Parse split up argument types, ensuring they are placed on subsequent
    # lines (except for the return type, which is n + 2 after the last arg line)
    # according to PEP 484
    # https://www.python.org/dev/peps/pep-0484/#suggested-syntax-for-python-2-7-and-straddling-code
    prev_line_num = None
    return_line = None
    parameter_type_lines = []
    for line_num, line in type_lines:
        if prev_line_num is not None:
            difference = line_num - prev_line_num
            if difference == 2:
                # 2 lines since last type line, so this must be the return type
                # line `(...) -> return_type`
                return_line = line
                break
            elif difference == 1:
                # Expected case, it's another parameter type (fall through)
                pass
            else:
                raise RuntimeError("Too many lines between '# type' annotations "
                                   "on line '{}' (expected 1 or 2, found {})".format(line, difference))

        parameter_type_lines.append(line)
        prev_line_num = line_num

    if not return_line:
        raise RuntimeError("Did not find return type line on multiline type annotation")

    types = []
    # Get each individual type from each argument declaration
    for type_line in parameter_type_lines:
        item_type = type_line[type_line.find(type_comment) + len(type_comment):]
        types.append(item_type.strip())

    parameter_types = ", ".join(types)

    # Stitch together the parameter and return type pieces into 1 long type line
    line_parts = return_line.split("...")
    return line_parts[0] + parameter_types + line_parts[1]


def split_type_line(type_line):
    """Splits the comment with the type annotation into parts for argument and return types.

    For example, for an input of:
        # type: (Tensor, torch.Tensor) -> Tuple[Tensor, Tensor]

    This function will return:
        ("(Tensor, torch.Tensor)", "Tuple[Tensor, Tensor]")

    """
    start_offset = len('# type:')
    try:
        arrow_pos = type_line.index('->')
    except ValueError:
        raise RuntimeError("Syntax error in type annotation (cound't find `->`)")
    return type_line[start_offset:arrow_pos].strip(), type_line[arrow_pos + 2:].strip()


def try_real_annotations(fn):
    """Tries to use the Py3.5+ annotation syntax to get the type."""
    try:
        sig = inspect.signature(fn)
    except ValueError:
        return None

    all_annots = [sig.return_annotation] + [p.annotation for p in sig.parameters.values()]
    if all(ann is sig.empty for ann in all_annots):
        return None

    def as_ann(ann):
        # sig.empty is really annoying so convert it to None
        return ann if ann is not sig.empty else None

    arg_types = [ann_to_type(as_ann(p.annotation))
                 for p in sig.parameters.values()]
    return_type = ann_to_type(as_ann(sig.return_annotation))
    return arg_types, return_type


def ann_to_type(ann):
    if ann is None:
        return TensorType.get()
    elif ann is torch.Tensor:
        return TensorType.get()
    elif is_tuple(ann):
        return TupleType([ann_to_type(a) for a in ann.__args__])
    elif is_list(ann):
        return ListType(ann_to_type(ann.__args__[0]))
    elif is_dict(ann):
        key = ann_to_type(ann.__args__[0])
        value = ann_to_type(ann.__args__[1])
        return DictType(key, value)
    elif ann is float:
        return FloatType.get()
    elif ann is int:
        return IntType.get()
    elif ann is str:
        return StringType.get()
    raise ValueError("Unknown type annotation: '{}'".format(ann.__name__))
