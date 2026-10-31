static op_list_names(out, filter, segment, offset, limit) {
  auto ea, end, f, name, seg, n, kind;
  n = 0;
  end = MaxEA();
  for (ea = first_head(MinEA(), end); ea != BADADDR && ea < end; ea = NextHead(ea, end)) {
    f = GetFlags(ea);
    if (!hasName(f)) continue;
    name = Name(ea);
    seg = SegName(ea);
    if (!matches(name, filter)) continue;
    if (segment != "" && seg != segment) continue;
    if (n >= offset && n < offset + limit) {
      kind = "data";
      if (isCode(f)) kind = "code";
      fprintf(out, "%s\t%s\t%s\t%s\n", hx(ea), esc(name), esc(seg), kind);
    }
    n = n + 1;
  }
  total(out, n);
  return 0;
}
