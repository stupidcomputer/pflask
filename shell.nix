{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  # Provide a Python interpreter with common packages available
  buildInputs = [
    (pkgs.python311.withPackages (ps: with ps; [
      flask
    ]))
  ];
}
