"""Immutable contract for the pinned upstream media-server-mcp tool surface.

The upstream Streamable HTTP server is allowed to advertise more wire fields
(such as outputSchema), but this companion consumes and verifies only the
reviewed projection below.  The compressed payload is still a checked-in
artifact: its decompressed canonical bytes and SHA-256 are verified at import.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, TypeAlias, cast


UPSTREAM_SOURCE_REVISION: Final[str] = "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
UPSTREAM_TOOL_CONTRACT_SHA256: Final[str] = (
    "65b3b6a3d439de558ba5c1f76cc755a2f05ca57474812c765313c654b509597e"
)
UPSTREAM_TOOL_DIGEST: Final[str] = "sha256:" + UPSTREAM_TOOL_CONTRACT_SHA256
CONTRACT_SCHEMA_VERSION: Final[int] = 1
TOOL_CONTRACT_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "title",
    "description",
    "inputSchema",
    "annotations",
)
# outputSchema is present in the captured wire response but is intentionally
# outside the consumed projection and therefore cannot expand the adapter.
WIRE_TOOL_FIELDS: Final[frozenset[str]] = frozenset(
    (*TOOL_CONTRACT_FIELDS, "outputSchema")
)


FrozenJson: TypeAlias = object
FrozenTool: TypeAlias = Mapping[str, FrozenJson]


# This is zlib-compressed canonical JSON plus one trailing newline.  Keeping
# the data compressed makes accidental hand edits obvious while preserving the
# exact per-tool schemas/descriptions/annotations in the source artifact.
_FROZEN_CANONICAL_B85: Final[str] = (
    "c-rk9U2oe)^8J3rLO8%Spx92Xhav{@kk(CWTysfdCw(XkVMVT_ttnFBuI%WF{P&yLU6M<3`6ZFEof;@oTOwz7XJ+Sn#((@ZqG1?G"
    "DtQ!&(b-?4XvM<cqd4$?;h`Lz&1fLlcoZ}0zYBwn{!qqi_H{h+nebx1!dFLUqf16(Z$V~JOqS6tXM*sMTrC)Ri@%X`D(RF8Ms7JJ"
    "AE-~`cr+gIaJ80~-hwSDF8)tpejF{NT#2)jlRrfiKGUBbNAdi`kLgUFyf}Sv`t13O6Z80Zw2EQhv4m9-Ou(01;t%;9zNKIIa=j$s"
    "dO2k=iDo2bVjW09WCTCTbsPdHxmhuISU)s^V~QKTDg*nk3kW_+4iN<_I_IIn>X8ekWbx9mL4_&FWxy1)6r&jkA!8oSM_<1pO+K%A"
    "%>2>W9~$0u`cyRilX(&s3h5F*h!w&o8sbFJ2Vvq$+mY_;xE1G=xa76(6RKCiBM!_6OCBP{U*A$5&?yhN+&HkVA0pIx%HSg8GE(>+"
    "j~Eb7VS0+o;L+Ei2gt{*|HwmFU5t<>0Rv3=YA5Xc2a6^N<;Eie7b%B(nNeULeW{Ar`hCIV0=Nt?WnLoiJ4xxMAy1<SRt*vK=QYLc"
    "zmKCC57>oYKJ9P%39{0Ukc)GKz81{Irg0R>H&NiT_&t@2GO!QuJ)jC70FIXnatFIL`y*lztplG-;U|4s2P!l}zKLSRX<hJ<U5*ST"
    "gfExO=Tx#_Lx4TtN$UHWeq~%qIv3?5UC}w<MBQe(S^*sR!Nxh4Y^h!=-)a2;@H5@0-}UXPx#TX4$fZAZ(amukFV0;w+W=y!RyFKN"
    ")H9`bi!w#;BsqwI?BKBypNyRsuUR|+u}ZSZEifG72hIyD<4JzYGA%W3CzZ|tFOsF}%;IP{v}l5}UMyEpEPwjq1#6-9VR66FyOrz@"
    "uLHN^`-G(cTwLy}qrCx2iB1dX_Y#Dt*InSbU&U)6SU`)USghwY0MpDKdP^k^sUe<&O}NhhZIXquGnaf|Gtl)5vSbp#OR9tqC1S-q"
    "KI2{{A_ifYePTM+CoQ469R~Wvx@DXg@g>me`X=#i+h>#Hv~EMK78fn8vsQNf&rA{;1eRrp>QJAW1biCPcrzICLGXYUcc|$%>I)L7"
    "--TtmmO#u|7_({!(+$y2ki#XFunR$GxH)oYdP_$aH2s1K6tHz`C|k?6Fx}F$LiuNKF5aI1kW>7YS=q+e%^)pZX0HH_S&LD#(OLmB"
    "d8h=alZ~1I{9LkDo!*qwlFOh-W79QJxU(&1EuoyY!xaU-qi|Vpp)v8`hCbt=V4>h>m>hMn_%h&EMWW}ey*Hro+EChU^=TMaQ7r`H"
    "Iak_^CAs$SU2p-mQyJzZT2w8hiVt$#ld08K0+D^6xsFtLBQ0H>ZsClxz{v*iu{s&Too;MjG;f%LO{lyq!T-nJh38z{R9NflP;yW^"
    "*G*9cy`!;p*s==y+Njj#OiofPCYr33R_R-f`Czv2z^O8k%gySb)`9NXoqJejMJEsO5KnRB6dfJtRv_CpSnQjUW)Uw6XMDbn6;!|)"
    "I6~;|7uALsgT(hqoJA|dP;_mF*4(}e=-vHj{o3CjS77O<?$v=Hf=LNG0d-o~%h-yTjR}+9@zMQU9}H-zUAwN&qlgLaJqXy|tTF+z"
    "4(8J~o{*+rN&f7{jQa=&lyus_F4EW5&OB_btcN=uqHhcekA~TDi3C%d2-)4qk_&-CFNr_h69#=Sr6#!59O63dy>A6*$RigN>?jp-"
    "Pdu;bS`t_XY;{o8(Onos6wJb7@)7I-x!}r3o^j9_HV6}|U(j37;f#gWn+hN<AtcAc>SG-bCfUJk&nE(&7tjA5o-iJSOKc&4OE7_R"
    "Kwg}V8@-2>%3{n}mA7!7xA_fZH0xPtvJCA#f!r(R7nfqdh5y4g`6Uy>ztK}D+xIQ3lRFwp=1&Zz3+72z#v7FLEBV!r!UK4e!Sg7-"
    "65ZgfWi*>L+yyU5Tdh~~SV4rRrl4nlJq(~H?*HbwU$aUL^S9x>Jm2+x6_B-)#R;%8+`|ou@;lT!z`N5a^T*cq#$*QOEdC#T2Axjv"
    "3A+W|E%c>OKVJco^vzK5pF?&$KPCqT-ocn0BoKAVPp7d8->7x)6NNI2j_Ndxe{eM1<w<v67t2x)5o82%Si({MMAqj9Z^hFbS!`{9"
    "A!9nB*<UDdm?tg3b6x`N{IPP1yeWrgUNi=ZGVj2iGZ)_JIy^e@W66)b$sglc4kLcdH7>?ninrVfP3}dixDtKiRMwysti=d;3O7v2"
    "9x*lvY7?SB(X$3>KbY%38uqF=@0a@eqMwXn0_(bcB(Dp14W3jq%Jh!Q1(;f1v_wu?EjEICGyu`d=Un0sI^IEnM(}x-_r&)AS(K=V"
    "Kup6q8<4d^2hDy$C5nq5UR=IIoGqywo8$ovoVgw$sWa<<A9sbd4&8-hPFB4z{As<Er8bIu+A@eq13-&T2wBXOl5%O69Q7-vOdcF8"
    "clIUmxlO1pV7007WAZ^|nFM?j0Jm!uWU*9Yvw?5}H3t7OhHkx<VRQv-aSL3i)0^66N>BnaFUi@f0-EGfSz9n)567qXCtn=Z3O~fr"
    "onS7)X!+7ftdvCv;F}yKSUz%~8J1ZC(XeXOu~Fm%by%S^-h9n{MPtKM7rw3}!tVwr8!qmst~AH^`6JLxZKeX>Wi4ioP=<M-zap(5"
    "RxpOzt-VSM4zVtWUAk3$N1AwKmvX-k48UR(S;)0_g9=9bMFb<-8QIet{CUmRAUWO{X&h|GG>E(#B|j|wKtL+}9J6Q1yTkEJx`k%p"
    "k2RE*WXj%bThaF$B0yuuohpbGMzIj#$AGg?HdgW)5-41!c=q(Wu3aHsKd|J<<|AJs9Vocnphp!`r4(`NyKRdR6>CfoGr8EX=C81p"
    "p|+NMZ=%>^PW_^mGa-A;w0lj!HL%GxXjX#5iR2O%KBueqoTj*n`FxJG<YY?W;zV=1_*^Fd@+OrP9W~K%a)r@G{hG?EScVIDB=IJB"
    "(pXLKdK7xYk?(8UQbd(BDg}Zs3+K1@F1F_Dw=}*%dp;$pNaIy^3l8f^O$d?X$n$LgsJxL~07T4I0rl7tUA@7Tw{e<W+V&UYPsPlO"
    "V&A27Er+c3vSf=i-<c)gaWYFnObN}3*iC#rbzM_b4`HSwajrfAsbW4{7VptEeWjQgr11+dY2D_O1dfZGQE0c`F6%rIxm3#!M|1G{"
    "*L2QmN*#VRkF;xe+PO6=!|(&`%`&v<)|{o~`%fz_BEMrFi6x*DHNgy=<`yk@8Q4OfvI|83WkkPg)q7QechtCC>Mtzf+T|`qm)l~Q"
    "sx`FWaMa2au5<2bn;l(OyjY~HNt*>glX$g|6yr30DQ2w_Js`|drcEN`_8XuUHmbVGT{7CWk1o>!yB1$xT2j+lSAAPuCwcX$qN2Je"
    "UT+IMaLvtc&B7Sl+)9$TT6hlC>z1vhjFN#x^&{zbOEA)@;mvTOj;^J6+{ODboMN2J60<bWaF7B}Bekuvx~$1#prE^6@xphA7o&?3"
    "5o0!Wl7_cC37Brhm+)V@j1p;yK5LFFSSM-{?s-Nq<&ft6Nv;aDP#alIr~P(A4s7o4^&TcAl%<Q<5($HKM5d-xKGpFp;XdcOOzgW>"
    "_F?MLv41L{F*g_$ik79zgiYMu)DU<2x(YoPJrOm9R<M0LP}-oa3xU-uL$tkS8rH;BH5wjN=WBZ1_ul!sTf=H^Eu|rocTs#(2gf{8"
    "Xwh^g*86WzEqZ2dSc&CdQJZ4U-wdF-iT9~d_Nh^Js7BceA@nUY%8nDm{l^GC8N6@Ss6$K-lnl<cYH2^Mv)L|y_stGW<L9USx6ih+"
    "R+{2McJu6~Et+Rb#G6bv6YjU_p>0NI-(*egsr@={Jqx{hLpX0FCcqCJwRPQ0V%XEXfWrNV<SK*vx)oPCJg63&&^P0yRf6fdRypCr"
    "uHiJLz9s^d4SwlZDV)S<l-bBby>wMn)4ZgaZ>!}}ZBf6_BQQsLE1_*C#hc1p%M3hdLB1{>dXR$I1m<s<>7E~*or066UmcyjJU#vX"
    "`OB9t{{7we-<`gE`8-8F9G=a4<CmKCme#h4Yq#Tt1o<{2Y+u;bngNU{&~_xmLl|~pU!a;{7dxM9AXJNuz_OTO6C5qANVSzo=G2$b"
    "m*4F`C|NKX$OVCucsEuL**#)L@5qf3=WYiYjG|TRK{kuRc0V4R`y)~t0t+v+a(rJvz7wz}MbfW>XZMNnbVW1y%7OoMBdwEPC11D="
    "0rukuC$=2MJNN7JW_i%Q^DdUJ(8_tu<6uvtR`M~B`x~`V$P;4KY|C6><sk;kRuw5GG|BEKkN+A2$H+-6pU}^hKyEo`$Xd5`@~J+*"
    "Sc$s9=HevP9S>|UPEwyzKNzRg<_8OD?J^rD599$&$LTE)@H7per)dC`$LC;%e3V|lGXX3$Tsx4bJHHQn<$4>+lLvdRV|kvg-QV}x"
    "IVS;iXZfdDKnE-fnST@0fXe6DbskU|V%<a_<NWQEeyAK((yPdOl5-Dd*pMATYd;Pqb&T#~+>qv^ESVE*j6gy$yD^#F$1qsvk>A+*"
    "OuJo(W~JKqSaN#Zr7mN;Sx5uq!?&7dW4K{}c{T~A9>P4EG=$Sr<THcUj#au(wFwi->}RlzW3P{KuuTQZcNlC_3$cg6HjZ1;Ww1>x"
    "n>@|7d75o=FSBj(ZHG^@Z3dccW6O@mGuy@yXzV=QCb41falTDH-_vQnO;;aihY2^PjN!3PxN-ZKyHB_=a39Qs8}u072kZ;F1;FUU"
    "qbvQ_VwAfIpJ1Q5wzW->PQCzh-^{;)y_%}VY@-DasOmh%h|IR%CnA4Edqdqd`~%J=39c4R?d(X)ertR8iNJJ+F6<Q!F@(2By*Ls="
    "p-e5AVJsUBJysnB3-DJo3r5P{_o7g0Q)KWO(j=~TdOGf2Q3^hg25?3IfU_1D)MC7G_P!#D)VD-UNUEG}gHfH@mN<hg71#mS>SCr6"
    "YUnm|6-7_A9#TR*WEU(XQNV1}?k)VI=<o^Nk5uUti-4=#qgCq48}#9?8+bKq`_1dN5}yv$GlWy|>i%4C%A{)2vu07fFJlx9!=+ej"
    "8qU*jYq1eXe@$-Kh8(U#{u!+=ANT<F&Eg~ESM@iz?0wevgk|epw~#Ig#+;Q`WsE9f4r(2m%BDgP%91>4z>li@Fl8Qt%bA-h+9exO"
    "#1lFq50=T17A@)=jpVv%L8zqKD=RxImU__~hI+kVNf-(z>uX`9gbq<7(Q|Y$7&s=E>y?_vZ(czD##pUf`#c)!UhuK%T-DlQ30D7r"
    "ks(~Ju{T{O9LMC~wK~IrvmvLE5fUDN2T$Cs=%SoOs!1bhOeaYsOHaXDM<$4>+EMcD3CA6~YgKV=iUGx2_&H`$kxKY~)yeu=9&{Nt"
    "|32LioMk3JtQ?*`!;VM4qTdGjqc+;Fo*;NN(%oL_&wgI;X~5mvlg@<7TP0FeLN>`cKYwKu#fw*3PyFkZPB4tgcdv|3c2w1`UAOTh"
    "ZdBQ=-BluFhT&$SZGckTxZ;)$V@!Kd5NO`75e)Yyptrb}&+G1`eO$-we7?o)?BNP+$w$^<EG|-{RaCUUA7b!{#i)6GpDjEX=VVJ%"
    "HEL&1Sf-kji3;e?f$m0!(v?P374Nf!oo%B;KLV9hUxT)y(u0<7uuAapkgHJ|$?gWWmmS{Hhm-;$Q;%k7pBej@XPVaYWsHRQ&+d|>"
    "VlYX8mnw!ymh#Q%uCmFH7J{1C%2NcM<ZzPkR7(85xCs*^QK%9)Xwg~J2?_fG;si<e7%%qtUU;%joOEIWoB}E%HxnSuvK_Z!g&tCd"
    "WhPOfT>3X+4xQ?|4{p#aPY~m7mM}!BCCosRVnI%X5{_u2-`>H{qtLeJi)^rKzjlF1kU9yK&Sou9=lUQ22eamN4F"
)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return value


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _deep_freeze(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(child) for child in value)
    return value


def _load_frozen_tools() -> tuple[FrozenTool, ...]:
    try:
        raw = zlib.decompress(base64.b85decode(_FROZEN_CANONICAL_B85))
        digest = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw.decode("utf-8", "strict"))
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        zlib.error,
        json.JSONDecodeError,
    ):
        raise RuntimeError(
            "pinned upstream tool contract artifact is invalid"
        ) from None
    if digest != UPSTREAM_TOOL_CONTRACT_SHA256:
        raise RuntimeError("pinned upstream tool contract artifact digest drifted")
    if not isinstance(value, list) or len(value) != 64:
        raise RuntimeError("pinned upstream tool contract must contain 64 tools")
    frozen: list[FrozenTool] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != set(TOOL_CONTRACT_FIELDS):
            raise RuntimeError("pinned upstream tool contract entry is invalid")
        if not isinstance(entry["name"], str) or not entry["name"]:
            raise RuntimeError("pinned upstream tool contract name is invalid")
        if not isinstance(entry["title"], str) or not entry["title"]:
            raise RuntimeError("pinned upstream tool contract title is invalid")
        if not isinstance(entry["description"], str):
            raise RuntimeError("pinned upstream tool contract description is invalid")
        if not isinstance(entry["inputSchema"], dict):
            raise RuntimeError("pinned upstream input schema is invalid")
        if not isinstance(entry["annotations"], dict):
            raise RuntimeError("pinned upstream tool annotations are invalid")
        frozen.append(cast(FrozenTool, _deep_freeze(entry)))
    names = tuple(cast(str, entry["name"]) for entry in frozen)
    if len(set(names)) != len(names):
        raise RuntimeError("pinned upstream tool contract names are duplicated")
    return tuple(frozen)


FROZEN_UPSTREAM_TOOLS: Final[tuple[FrozenTool, ...]] = _load_frozen_tools()
FROZEN_UPSTREAM_TOOL_NAMES: Final[tuple[str, ...]] = tuple(
    cast(str, entry["name"]) for entry in FROZEN_UPSTREAM_TOOLS
)
FROZEN_UPSTREAM_TOOL_SET: Final[frozenset[str]] = frozenset(FROZEN_UPSTREAM_TOOL_NAMES)


def canonical_tool_projection(
    tools: Sequence[Mapping[str, object]],
) -> bytes:
    """Return the exact pinned projection used for the contract digest."""

    projected: list[dict[str, object]] = []
    for entry in tools:
        if not isinstance(entry, Mapping):
            raise ValueError("upstream tool entry is not an object")
        projected.append(
            {field: _thaw(entry.get(field)) for field in TOOL_CONTRACT_FIELDS}
        )
    try:
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("upstream tool projection is not valid JSON") from None
    return encoded + b"\n"


def canonical_tool_digest(tools: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(canonical_tool_projection(tools)).hexdigest()


def validate_live_tools(
    tools: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Validate the live list against the frozen names and reviewed fields."""

    if len(tools) != len(FROZEN_UPSTREAM_TOOLS):
        raise ValueError("upstream tool count does not match the pinned contract")
    names: list[str] = []
    for entry in tools:
        if not isinstance(entry, Mapping):
            raise ValueError("upstream tool entry is not an object")
        keys = set(entry)
        if not set(TOOL_CONTRACT_FIELDS).issubset(keys):
            raise ValueError("upstream tool entry is missing reviewed fields")
        if not keys.issubset(WIRE_TOOL_FIELDS):
            raise ValueError("upstream tool entry has an unreviewed field")
        name = entry.get("name")
        title = entry.get("title")
        description = entry.get("description")
        schema = entry.get("inputSchema")
        annotations = entry.get("annotations")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(title, str)
            or not title
            or not isinstance(description, str)
            or not isinstance(schema, Mapping)
            or not isinstance(annotations, Mapping)
        ):
            raise ValueError("upstream tool reviewed fields have invalid types")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("upstream tool names are duplicated")
    if tuple(names) != FROZEN_UPSTREAM_TOOL_NAMES:
        raise ValueError("upstream tool names do not match the pinned contract")
    if canonical_tool_digest(tools) != UPSTREAM_TOOL_CONTRACT_SHA256:
        raise ValueError("upstream tool schemas drifted from the pinned contract")
    return FROZEN_UPSTREAM_TOOL_NAMES


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "FROZEN_UPSTREAM_TOOL_NAMES",
    "FROZEN_UPSTREAM_TOOL_SET",
    "FROZEN_UPSTREAM_TOOLS",
    "TOOL_CONTRACT_FIELDS",
    "UPSTREAM_SOURCE_REVISION",
    "UPSTREAM_TOOL_CONTRACT_SHA256",
    "UPSTREAM_TOOL_DIGEST",
    "WIRE_TOOL_FIELDS",
    "canonical_tool_digest",
    "canonical_tool_projection",
    "validate_live_tools",
]
