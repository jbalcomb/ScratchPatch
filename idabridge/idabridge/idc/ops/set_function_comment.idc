static op_set_function_comment(out, ea, name, text, repeatable) {
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  if (GetFunctionAttr(ea, FUNCATTR_START) == BADADDR) return err(out, "address is not inside a function");
  SetFunctionCmt(ea, text, repeatable);
  info(out, "function", GetFunctionName(ea));
  return 0;
}
