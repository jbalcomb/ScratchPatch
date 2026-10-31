static op_undefine(out, ea, name) {
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  MakeUnkn(ea, 0);
  info(out, "ea", hx(ea));
  return 0;
}
