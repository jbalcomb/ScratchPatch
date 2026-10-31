static op_list_entry_points(out) {
  auto i, n, ord;
  n = GetEntryPointQty();
  for (i = 0; i < n; i++) {
    ord = GetEntryOrdinal(i);
    fprintf(out, "%d\t%s\t%s\n", ord, hx(GetEntryPoint(ord)), esc(GetEntryName(ord)));
  }
  total(out, n);
  return 0;
}
