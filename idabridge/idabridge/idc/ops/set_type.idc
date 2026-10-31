static op_set_type(out, ea, name, decl) {
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  if (!SetType(ea, decl)) return err(out, "IDA could not parse or apply the declaration");
  info(out, "ea", hx(ea));
  return 0;
}
