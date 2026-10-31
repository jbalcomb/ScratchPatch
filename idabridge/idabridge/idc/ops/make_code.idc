static op_make_code(out, ea, name) {
  auto len;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  len = MakeCode(ea);
  if (len == 0) return err(out, "IDA could not make an instruction here");
  Wait();
  info(out, "ea", hx(ea));
  info(out, "length", form("%d", len));
  info(out, "disasm", GetDisasm(ea));
  return 0;
}
