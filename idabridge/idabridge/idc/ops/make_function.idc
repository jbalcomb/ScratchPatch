static op_make_function(out, ea, name, end) {
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  if (!MakeFunction(ea, end)) return err(out, "IDA could not create a function here");
  Wait();
  info(out, "name", GetFunctionName(ea));
  info(out, "start", hx(GetFunctionAttr(ea, FUNCATTR_START)));
  info(out, "end", hx(GetFunctionAttr(ea, FUNCATTR_END)));
  return 0;
}
