// kind is one of the OFILE_* constants; path is inside the job folder (chosen by the server).
static op_export(out, kind, path, start, end) {
  auto h, mode, r;
  if (start == BADADDR) start = MinEA();
  if (end == BADADDR) end = MaxEA();
  mode = "w";
  if (kind == OFILE_EXE || kind == OFILE_DIF) mode = "wb";
  h = fopen(path, mode);
  if (h == 0) return err(out, "could not open the output file");
  r = GenerateFile(kind, h, start, end, 0);
  fclose(h);
  if (r < 0) return err(out, "IDA could not produce this kind of file for this input");
  info(out, "lines", form("%d", r));
  return 0;
}
