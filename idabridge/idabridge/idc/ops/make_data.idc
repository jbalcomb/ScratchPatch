static op_make_data(out, ea, name, kind) {
  auto ok;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  ok = 0;
  if (kind == "byte")        ok = MakeByte(ea);
  else if (kind == "word")   ok = MakeWord(ea);
  else if (kind == "dword")  ok = MakeDword(ea);
  else if (kind == "qword")  ok = MakeQword(ea);
  else if (kind == "float")  ok = MakeFloat(ea);
  else if (kind == "double") ok = MakeDouble(ea);
  else if (kind == "string") ok = MakeStr(ea, BADADDR);
  else return err(out, "unknown data kind");
  if (!ok) return err(out, "IDA could not convert the item");
  info(out, "ea", hx(ea));
  info(out, "disasm", GetDisasm(ea));
  return 0;
}
