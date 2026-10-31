static op_list_segments(out) {
  auto ea, n;
  n = 0;
  for (ea = FirstSeg(); ea != BADADDR; ea = NextSeg(ea)) {
    fprintf(out, "%s\t%s\t%s\t%d\n", esc(SegName(ea)), hx(SegStart(ea)), hx(SegEnd(ea)), SegEnd(ea) - SegStart(ea));
    n = n + 1;
  }
  total(out, n);
  return 0;
}
