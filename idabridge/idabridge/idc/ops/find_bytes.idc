static op_find_bytes(out, pattern, start, limit) {
  auto ea, n;
  n = 0;
  ea = FindBinary(start, SEARCH_DOWN, pattern);
  while (ea != BADADDR && n < limit) {
    fprintf(out, "%s\t%s\t%s\n", hx(ea), esc(SegName(ea)), esc(GetFunctionName(ea)));
    n = n + 1;
    ea = FindBinary(ea, SEARCH_DOWN | SEARCH_NEXT, pattern);
  }
  total(out, n);
  return 0;
}
