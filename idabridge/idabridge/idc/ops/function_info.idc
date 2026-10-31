static op_function_info(out, ea, name) {
  auto start, end, x, item, target, n;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  start = GetFunctionAttr(ea, FUNCATTR_START);
  if (start == BADADDR) return err(out, "address is not inside a function");
  end = GetFunctionAttr(start, FUNCATTR_END);
  info(out, "name", GetFunctionName(start));
  info(out, "start", hx(start));
  info(out, "end", hx(end));
  info(out, "size", form("%d", end - start));
  info(out, "flags", hx(GetFunctionFlags(start)));
  info(out, "frame_size", form("%d", GetFrameSize(start)));
  info(out, "comment", GetFunctionCmt(start, 0));
  info(out, "repeatable_comment", GetFunctionCmt(start, 1));
  n = 0;
  for (x = RfirstB0(start); x != BADADDR; x = RnextB0(start, x)) {
    fprintf(out, "caller\t%s\t%s\n", hx(x), esc(GetFunctionName(x)));
    n = n + 1;
  }
  for (item = start; item != BADADDR && item < end; item = NextHead(item, end)) {
    if (!isCode(GetFlags(item))) continue;
    for (x = Rfirst0(item); x != BADADDR; x = Rnext0(item, x)) {
      target = GetFunctionAttr(x, FUNCATTR_START);
      if (target == x && target != start) {
        fprintf(out, "callee\t%s\t%s\n", hx(x), esc(Name(x)));
        n = n + 1;
      }
    }
  }
  total(out, n);
  return 0;
}
