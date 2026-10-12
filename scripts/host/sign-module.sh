#!/usr/bin/env bash
# Sign an out-of-tree kernel module with this machine's own Secure Boot key (MOK).
#
#   sudo scripts/host/sign-module.sh [options] <module name | path/to/module.ko[.zst|.xz|.gz]>...
#
#   --key FILE      private key (PEM or DER)         default: auto-detected, see below
#   --cert FILE     matching certificate (DER)       default: auto-detected
#   --kernel REL    kernel release to sign for       default: the running kernel (uname -r)
#   --create-key    create a new key pair when none exists (in /var/lib/superfarm/mok)
#   --enroll        ask the firmware to enrol the certificate (sets a one-time password,
#                   takes effect at the next reboot)
#   --status        only report: Secure Boot state, key found, enrolled or not
#
# Why this exists: with Secure Boot on, the kernel refuses every module that is not signed
# by a key the firmware trusts. Distribution kernels are signed by the distribution; a
# module you build yourself (the virtual camera module, v4l2loopback) is not. The fix is
# a "Machine Owner Key": a key pair you create, enrol once in the firmware, and sign your
# modules with. It must be repeated after every kernel update, because the module is
# rebuilt for the new kernel.
#
# Key locations tried, in order (the first complete pair wins):
#   /var/lib/shim-signed/mok/MOK.{priv,der}             Debian / Ubuntu
#   /var/lib/dkms/mok.{key,pub}                         DKMS 3.x on any distribution
#   /etc/pki/akmods/private/private_key.priv + /etc/pki/akmods/certs/public_key.der   Fedora / RHEL (akmods)
#   /var/lib/superfarm/mok/MOK.{priv,der}               created by --create-key
#
# Nothing here loads a module. Loading is done by `sudo sf host apply`.
set -euo pipefail

KEY=""; CERT=""; KREL=$(uname -r); CREATE=0; ENROLL=0; STATUS=0
TARGETS=()
OWN_DIR=/var/lib/superfarm/mok

die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --key)        [ $# -ge 2 ] || die "--key needs a file"; KEY=$2; shift ;;
    --cert)       [ $# -ge 2 ] || die "--cert needs a file"; CERT=$2; shift ;;
    --kernel)     [ $# -ge 2 ] || die "--kernel needs a release"; KREL=$2; shift ;;
    --create-key) CREATE=1 ;;
    --enroll)     ENROLL=1 ;;
    --status)     STATUS=1 ;;
    -h|--help)    sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --)           shift; TARGETS+=("$@"); break ;;
    -*)           die "unknown option $1 (see --help)" ;;
    *)            TARGETS+=("$1") ;;
  esac
  shift
done
case "$KREL" in *[!A-Za-z0-9._+~-]*|"") die "bad kernel release '$KREL'" ;; esac

# ------------------------------------------------------------------ Secure Boot state
sb_state() {
  if command -v mokutil >/dev/null 2>&1; then
    local out
    out=$(mokutil --sb-state 2>/dev/null || true)
    case "$out" in *[Ee]nabled*) echo on ;; *) echo off ;; esac
  elif [ -d /sys/firmware/efi ]; then
    # Last byte of the SecureBoot EFI variable: 1 = enabled.
    f=$(ls /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null | head -n 1 || true)
    if [ -n "$f" ] && [ "$(od -An -t u1 -j 4 -N 1 "$f" 2>/dev/null | tr -d ' ')" = 1 ]; then echo on; else echo off; fi
  else
    echo off
  fi
}

# ------------------------------------------------------------------ key pair
find_keys() {
  local pair
  for pair in \
      "/var/lib/shim-signed/mok/MOK.priv:/var/lib/shim-signed/mok/MOK.der" \
      "/var/lib/dkms/mok.key:/var/lib/dkms/mok.pub" \
      "/etc/pki/akmods/private/private_key.priv:/etc/pki/akmods/certs/public_key.der" \
      "$OWN_DIR/MOK.priv:$OWN_DIR/MOK.der"; do
    if [ -f "${pair%%:*}" ] && [ -f "${pair##*:}" ]; then
      KEY=${pair%%:*}; CERT=${pair##*:}
      return 0
    fi
  done
  return 1
}

create_keys() {
  command -v openssl >/dev/null 2>&1 || die "openssl is needed to create a key (install the openssl package)"
  install -d -m 700 "$OWN_DIR"
  local cnf
  cnf=$(mktemp)
  # extendedKeyUsage: code signing, restricted to kernel modules (OID 1.3.6.1.4.1.2312.16.1.2),
  # so this key cannot be used to sign a boot loader or kernel.
  cat >"$cnf" <<'EOF'
[ req ]
default_bits = 2048
distinguished_name = dn
prompt = no
x509_extensions = ext
[ dn ]
CN = Super Farm module signing key
[ ext ]
basicConstraints = critical,CA:FALSE
keyUsage = digitalSignature
extendedKeyUsage = codeSigning,1.3.6.1.4.1.2312.16.1.2
subjectKeyIdentifier = hash
EOF
  ( umask 077
    openssl req -new -x509 -newkey rsa:2048 -nodes -days 36500 -config "$cnf" \
      -keyout "$OWN_DIR/MOK.priv" -outform DER -out "$OWN_DIR/MOK.der" >/dev/null 2>&1 ) \
    || { rm -f "$cnf"; die "openssl could not create the key pair"; }
  rm -f "$cnf"
  chmod 600 "$OWN_DIR/MOK.priv"; chmod 644 "$OWN_DIR/MOK.der"
  KEY=$OWN_DIR/MOK.priv; CERT=$OWN_DIR/MOK.der
  note "created $KEY and $CERT"
}

enrolled() {
  command -v mokutil >/dev/null 2>&1 || return 2
  # Judged by the text, not the exit status: mokutil exits non-zero for an enrolled key
  # when it cannot also read the kernel keyring (seen on Ubuntu 26.04).
  local out
  out=$(mokutil --test-key "$CERT" 2>&1 || true)
  case "$out" in *"already enrolled"*) return 0 ;; esac
  return 1
}

enrol_help() {
  cat <<EOF

The certificate is not enrolled in the firmware yet, so modules signed with it are still
refused. Enrol it once:

  1. sudo mokutil --import $CERT
       (asks you to choose a one-time password; any short password will do)
  2. Reboot. A blue "MOK management" screen appears before the system starts:
       Enroll MOK -> Continue -> Yes -> type the password from step 1 -> Reboot
  3. Back in the system, check:   mokutil --test-key $CERT
       it must say "is already enrolled"
  4. Run this script again to sign, then:   sudo sf host apply

If the blue screen never appears, the firmware skipped it (fast boot): run step 1 again
and reboot from the firmware's boot menu. Without mokutil: install the "mokutil" package.
EOF
}

# ------------------------------------------------------------------ sign-file
find_sign_file() {
  local c
  for c in "/lib/modules/$KREL/build/scripts/sign-file" \
           "/usr/lib/modules/$KREL/build/scripts/sign-file" \
           "/usr/src/linux-headers-$KREL/scripts/sign-file" \
           "/usr/src/kernels/$KREL/scripts/sign-file" \
           "/usr/src/linux-obj/$(uname -m)/${KREL##*-}/scripts/sign-file"; do
    if [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  if command -v kmodsign >/dev/null 2>&1; then command -v kmodsign; return 0; fi
  return 1
}

sig_hash() {
  # Sign with the digest this kernel was configured for; sha256 is accepted everywhere as a fallback.
  local cfg h=""
  for cfg in "/boot/config-$KREL" "/lib/modules/$KREL/config" "/lib/modules/$KREL/build/.config"; do
    if [ -r "$cfg" ]; then
      h=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"$/\1/p' "$cfg" | head -n 1)
      break
    fi
  done
  case "$h" in sha1|sha224|sha256|sha384|sha512) echo "$h" ;; *) echo sha256 ;; esac
}

resolve_module() {
  local t=$1 path
  if [ -f "$t" ]; then readlink -f "$t"; return 0; fi
  case "$t" in */*|*.ko*) die "no such file: $t" ;; esac
  case "$t" in *[!A-Za-z0-9_-]*) die "bad module name '$t'" ;; esac
  path=$(modinfo -k "$KREL" -n "$t" 2>/dev/null || true)
  if [ -z "$path" ] || [ ! -f "$path" ]; then
    die "module '$t' is not installed for kernel $KREL. Build and install it first (for the camera module: sudo native/v4l2loopback/build.sh install)."
  fi
  echo "$path"
}

sign_one() {
  local path=$1 tool=$2 hash=$3 work plain comp="" signer
  work=$(mktemp -d "$WORK/m.XXXXXX")
  case "$path" in
    *.ko.zst) comp=zst; plain="$work/$(basename "${path%.zst}")"; zstd -q -d -o "$plain" "$path" ;;
    *.ko.xz)  comp=xz;  plain="$work/$(basename "${path%.xz}")";  xz -d -c "$path" >"$plain" ;;
    *.ko.gz)  comp=gz;  plain="$work/$(basename "${path%.gz}")";  gzip -d -c "$path" >"$plain" ;;
    *.ko)     plain="$work/$(basename "$path")"; cp -- "$path" "$plain" ;;
    *)        die "$path does not look like a kernel module (.ko, .ko.zst, .ko.xz, .ko.gz)" ;;
  esac
  # Signing twice with the same key only grows the file. A signature from another key
  # (DKMS signs with its own, often un-enrolled one) is fine to sign over: the kernel
  # checks the last signature appended.
  if tail -c 28 "$plain" | grep -qa 'Module signature appended'; then
    signer=$(modinfo -F signer "$plain" 2>/dev/null || true)
    if [ -n "$signer" ] && [ "$signer" = "$(cert_cn)" ]; then
      note "already signed with this key: $path"
      return 0
    fi
  fi
  # sign-file and Ubuntu's kmodsign take the same arguments.
  "$tool" "$hash" "$KEY" "$CERT" "$plain"
  case "$comp" in
    zst) zstd -q -f -o "$work/out" "$plain" ;;
    xz)  xz -c "$plain" >"$work/out" ;;
    gz)  gzip -c "$plain" >"$work/out" ;;
    "")  cp -- "$plain" "$work/out" ;;
  esac
  # Replace in one step, keeping owner and mode of the original.
  install -m "$(stat -c %a "$path")" -o "$(stat -c %u "$path")" -g "$(stat -c %g "$path")" "$work/out" "$path.new"
  mv -f -- "$path.new" "$path"
  note "signed ($hash): $path   signer: $(modinfo -F signer "$path" 2>/dev/null || echo '?')"
}

cert_cn() {
  openssl x509 -inform DER -in "$CERT" -noout -subject -nameopt multiline 2>/dev/null \
    | sed -n 's/^ *commonName *= *//p' | head -n 1
}

# ------------------------------------------------------------------ main
SB=$(sb_state)
if [ -z "$KEY$CERT" ]; then
  find_keys || true
elif [ -z "$KEY" ] || [ -z "$CERT" ]; then
  die "--key and --cert must be given together"
fi

if [ "$STATUS" = 1 ]; then
  note "Secure Boot: $SB"
  if [ -n "$KEY" ]; then
    note "key pair:    $KEY + $CERT"
    rc=0; enrolled || rc=$?
    case "$rc" in 0) note "enrolled:    yes" ;; 1) note "enrolled:    no" ;; *) note "enrolled:    unknown (mokutil is not installed)" ;; esac
  else
    note "key pair:    none found (create one with --create-key)"
  fi
  if sf=$(find_sign_file); then note "sign tool:   $sf"; else note "sign tool:   not found (install the kernel headers for $KREL)"; fi
  exit 0
fi

[ "$(id -u)" = 0 ] || die "run with sudo: the signing key is readable by root only."

if [ "$SB" = off ] && [ "$CREATE" = 0 ] && [ "$ENROLL" = 0 ]; then
  note "Secure Boot is off: this kernel loads unsigned modules, nothing needs signing."
  [ ${#TARGETS[@]} -gt 0 ] || exit 0
  note "Signing anyway, as asked."
fi

if [ -z "$KEY" ]; then
  if [ "$CREATE" = 1 ]; then
    create_keys
  else
    die "no signing key found. Create one with:  sudo $0 --create-key --enroll
(on Debian/Ubuntu 'sudo update-secureboot-policy --new-key' creates the distribution's own in /var/lib/shim-signed/mok)"
  fi
fi
[ -r "$KEY" ] || die "cannot read $KEY"
[ -r "$CERT" ] || die "cannot read $CERT"

if [ "$ENROLL" = 1 ]; then
  command -v mokutil >/dev/null 2>&1 || die "mokutil is not installed (package: mokutil)"
  if enrolled; then
    note "certificate is already enrolled"
  else
    note "Choose a one-time password; you will type it once at the next boot."
    mokutil --import "$CERT"
    note "Enrolment requested. Reboot and choose 'Enroll MOK' on the blue screen."
  fi
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
  [ "$CREATE" = 1 ] || [ "$ENROLL" = 1 ] || die "nothing to sign: name a module or a .ko file (see --help)"
  exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
TOOL=$(find_sign_file) || die "sign-file not found for kernel $KREL. Install the kernel headers:
  Debian/Ubuntu: apt-get install linux-headers-$KREL      Fedora/RHEL: dnf install kernel-devel-$KREL
  Arch: pacman -S linux-headers                            openSUSE: zypper install kernel-default-devel"
HASH=$(sig_hash)

for t in "${TARGETS[@]}"; do
  sign_one "$(resolve_module "$t")" "$TOOL" "$HASH"
done
# modules.dep does not change by signing, but a freshly installed module may not be indexed yet.
depmod -a "$KREL" 2>/dev/null || true

if [ "$SB" = on ]; then
  rc=0; enrolled || rc=$?
  if [ "$rc" = 1 ]; then
    enrol_help
    exit 3
  elif [ "$rc" = 2 ]; then
    note "mokutil is not installed, so enrolment could not be checked. If the module is refused at load time, the key is not enrolled."
  fi
fi
note "Done. Load it with:  sudo sf host apply    (repeat the signing after every kernel update)"
