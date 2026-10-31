static op_xrefs_to(out, ea, name, limit) {
  auto x, n;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  n = 0;
  for (x = RfirstB0(ea); x != BADADDR; x = RnextB0(ea, x)) {
    if (n < limit) fprintf(out, "code\t%s\t%s\t%s\n", hx(x), esc(GetFunctionName(x)), esc(GetDisasm(x)));
    n = n + 1;
  }
  for (x = DfirstB(ea); x != BADADDR; x = DnextB(ea, x)) {
    if (n < limit) fprintf(out, "data\t%s\t%s\t%s\n", hx(x), esc(GetFunctionName(x)), esc(GetDisasm(x)));
    n = n + 1;
  }
  total(out, n);
  return 0;
}
