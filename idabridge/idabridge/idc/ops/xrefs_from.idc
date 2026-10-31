static op_xrefs_from(out, ea, name, limit) {
  auto x, n;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  n = 0;
  for (x = Rfirst0(ea); x != BADADDR; x = Rnext0(ea, x)) {
    if (n < limit) fprintf(out, "code\t%s\t%s\n", hx(x), esc(Name(x)));
    n = n + 1;
  }
  for (x = Dfirst(ea); x != BADADDR; x = Dnext(ea, x)) {
    if (n < limit) fprintf(out, "data\t%s\t%s\n", hx(x), esc(Name(x)));
    n = n + 1;
  }
  total(out, n);
  return 0;
}
