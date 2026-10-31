static op_info(out) {
  auto i, c, proc, n, ea;
  proc = "";
  for (i = 0; i < 8; i++) {
    c = GetCharPrm(INF_PROCNAME + i);
    if (c == 0) break;
    proc = proc + form("%c", c);
  }
  info(out, "input_file", GetInputFile());
  info(out, "processor", proc);
  info(out, "min_ea", hx(MinEA()));
  info(out, "max_ea", hx(MaxEA()));
  info(out, "start_ea", hx(BeginEA()));
  n = 0;
  for (ea = FirstSeg(); ea != BADADDR; ea = NextSeg(ea)) n = n + 1;
  info(out, "segments", form("%d", n));
  n = 0;
  ea = MinEA();
  if (GetFunctionAttr(ea, FUNCATTR_START) != ea) ea = NextFunction(ea);
  for (; ea != BADADDR; ea = NextFunction(ea)) n = n + 1;
  info(out, "functions", form("%d", n));
  info(out, "entry_points", form("%d", GetEntryPointQty()));
  return 0;
}
