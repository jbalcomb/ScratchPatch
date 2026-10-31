static op_reanalyze(out, start, end) {
  if (end == BADADDR) end = MaxEA();
  if (start == BADADDR) start = MinEA();
  AnalyzeArea(start, end);
  Wait();
  info(out, "start", hx(start));
  info(out, "end", hx(end));
  return 0;
}
