static op_rename(out, ea, name, new_name) {
  auto old;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  old = Name(ea);
  if (!MakeNameEx(ea, new_name, SN_CHECK | SN_NOWARN)) return err(out, "IDA refused the name (invalid or already used)");
  info(out, "ea", hx(ea));
  info(out, "old_name", old);
  info(out, "new_name", Name(ea));
  return 0;
}
