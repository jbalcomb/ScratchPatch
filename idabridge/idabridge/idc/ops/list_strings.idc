static op_list_strings(out, filter, min_length, offset, limit) {
  auto ea, end, f, s, n, t;
  n = 0;
  end = MaxEA();
  for (ea = first_head(MinEA(), end); ea != BADADDR && ea < end; ea = NextHead(ea, end)) {
    f = GetFlags(ea);
    if (!isASCII(f)) continue;
    t = GetStringType(ea);
    s = GetString(ea, -1, t);
    if (strlen(s) < min_length) continue;
    if (!matches(s, filter)) continue;
    if (n >= offset && n < offset + limit)
      fprintf(out, "%s\t%d\t%d\t%s\t%s\n", hx(ea), strlen(s), t, esc(SegName(ea)), esc(s));
    n = n + 1;
  }
  total(out, n);
  return 0;
}
