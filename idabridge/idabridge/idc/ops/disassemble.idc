// whole_function: 1 = from the start to the end of the function holding ea; 0 = `count` items from ea.
static op_disassemble(out, ea, name, count, whole_function) {
  auto end, n, f, cmt;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  end = MaxEA();
  if (whole_function) {
    if (GetFunctionAttr(ea, FUNCATTR_START) == BADADDR) return err(out, "address is not inside a function");
    end = GetFunctionAttr(ea, FUNCATTR_END);
    ea = GetFunctionAttr(ea, FUNCATTR_START);
    info(out, "function", GetFunctionName(ea));
  }
  if (isTail(GetFlags(ea))) ea = PrevHead(ea, MinEA());
  n = 0;
  while (ea != BADADDR && ea < end && n < count) {
    cmt = Comment(ea);
    if (cmt == "") cmt = RptCmt(ea);
    fprintf(out, "%s\t%s\t%s\t%s\t%s\n", hx(ea), item_bytes(ea, 16), esc(Name(ea)), esc(GetDisasm(ea)), esc(cmt));
    n = n + 1;
    ea = NextNotTail(ea);
  }
  if (ea != BADADDR && ea < end) info(out, "next_ea", hx(ea));
  total(out, n);
  return 0;
}
