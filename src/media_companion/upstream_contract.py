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
    "31451102af4d424ce516d5515db5839028f17e9363651e0d1a2d64518633f2b1"
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
    "c-rk<ZExE+w*G#9g<xDD3E;FzJ3E6Gm=B$1IvebCr<<f5Tr3KMwrHChS#l*hsdv%;e$U~HL`l?}E!s}kgTahrONZn+56>IP!+-u~"
    "K*BKMgtI722Cx4!h?X?`Gl~QMuPo$)*HaQCbU279@&5>ewfvIDEBb9X@M$uM*%DtJydK<A5>MvdG>W}N^uTE1vCzAlQ|~?g<y{d@"
    "#w4NM10&uI@ktyHh65HZSNwJ|rwf9O|8Jr`4(6OMlh@Cm|0{{YbNTJFD4sp{V>0E>UtGMnIKO=HTs=M<EMw?*%%N2z6R-v2_#xlJ"
    "_v9;EtQKClT8wGzMN==P$tvK9$0PX2S8)iW_<Bj<VfoMihMC;6WtrIDZA9=<?-W_EBr_HYuAbRwavCoz9Tc46j0aRuOF5d6NO;V`"
    "+2GqZl*yMBi>W_&{ZC2nbNW;?{uiBaY$zlP{2~?%A4!TMAs?iPEo~;cZ^Kr|DWT+z?|VeHf<tr+gar#x;%^=Z3&@xSjIS+Rmk)Ww"
    "cuMgiWIPi59u6o7PjGsS&EU{i;RKkEU4P3$XgwLAOacm=@<mVR)h~ujwB&{ZMHeN9eVGz~kDOFRZv8o@d=4lBPN|nX_}fe6Cn=Ak"
    "2wDx1^p_RE?th4*DGTVeUmop0<P+XfKH^<pA@x;4Z7_|ah`);hpT-{upO=Z<!0*5+d;k#7=iVdqR`t(I=Fuwfy)k@}qqVR?0`grH"
    "3#4`7M>aV!mUwKjpgtp<25S%C0Z$U&m+~v~ij!GV9?~6|0Z+tiCd(zzfe$9n8K(>JT6s>(F96SEE&i6Xt7ekhIKmhH*d{m2biBT@"
    "$!rabscKch(-Y5>-Yx1B*%LhwMcIO}5>AHJiPx;2Kv*T+WLFp#^#kJt$V@nUpqZ5#w$n;iz!y%_cBXN(aIKnPtk;WW6!TxdPJ&e;"
    "`eC47+1*O`!|Q-{eBYxf0UOGFF|>Cesiac_@;%KE@w$yX^Q*81iUq7l%EfxD0hwla$S#!-QX@PIOqfrBZJdU@GfHmg6m0$6TTl+<"
    "IT6Z-c*&AZ*py8&72yOhyX15%M_OWaI}Y^Ab;CJP>MKF7>$~K;>7R}CXzh+#EiYPnXRZAD-zfJ;5Ez~znnO8iFJR-C#A|2F2gL(c"
    "++wERi66X3{GAxCYl*~+hB2+CFkXA|3GZ}4IP@a%NVq<;SbD=o7cBjpB&cBP+7P~$>0#QnX@&96=v=?Q`Xy)h4Y#t5vzt*`*xcR("
    "5VJ}K%}#3tP5n@bP$wTXBlxA{ty-fgt0$LH;mW6Ll5l5V&S*jeZKq2DxFaxGu%V$hff;(v!i0tiL&xN-i^rD{zbgto@9n(<i`Pcd"
    "cCSy;xQl8jsK~j}Z!GDx!w<m|@SVyuZ_%S_p;dg)>l2=OZ6y-f?^)<blF2}7S8G@}Wi+tD0KOI@L%QR&8H?r}bMOg8l$Ef5(?7y1"
    "mfTl(>#LA6Fgu@{vI??CLu0UI6ZWmqsLd!JrCijOtd&*i+l=|(wy?lzG7;sfbujB7_iWA`mRr#&LOjJ$oLN;z2e}o*wh0&emZWNA"
    "5`|MXTg8GZa18<>4EKv>BN>9m_q{mFRwS<E+K#Qceizug{m}aLzdtU)(@(>zlf+9X=g<={r-i-@jf~mQqkQu0Y(LWn6I!Czw(0XE"
    "a)LWg0=9RnRD!I7`?N_UB;{AqKih$E7wLeKO>-O~eQO=eL)Xf7xZ@!D=3p`*Vb+}1V4@QteSE%PNrFmG=b!e(K_6VH5q34lxK4fV"
    "+W;!@@HqiLN~GL%<TYM#5844=9ZYrf7zPmmxA2+w5&Qu@XTnLIGO!sY36-m#lLxTjl!nHef*>}LaE61G!+PdSvxD27UkJD?j{gHZ"
    "p&|&I+Cl=i-~#7}yto)PMh{D|iZP>A(ZW^U=U0@`t*4<{WoRD><X%yK*bD<U{5QJJH>n-}#z>*;-?#8i9!bclKT?t|xF=nK=M?#s"
    "c=e<31RkaEJgP5k8zw6rO{WcWVWO9<R?Ar|D8f@?urmM;Md*nB?;iaUDmCD5gS|ZNdY=jiZQZyBaE5ufMpgcZb_cLK9#em4bZ_WQ"
    "!JWnbW6U6zQ+$s;fbCA?q!6F)fJt&@i2TnfeKvdMohW)IL+?aWluLec8LRM%Xa~O$EW_-mT&D3)&fHC&wD)znEb)+sK=4jWJj##A"
    "_+Ig?IGQs9)`}Pc(@M?!L6F0B(gHo}B#^-$EiaKbt>LK`m4hPOJMib!gtvwcPfq<9`eSGMM@5!y)Q_6R#hgoVmK&+booE%?Lf<ge"
    "HRuIPH3E^sJr%kq8L9<pm7+k^vjS-!%=90)ood$kC4Ro{XGKxd`rJN~*9Be97ZsH;ePnzNuGS=4Ad;5zb;2ej0M*N8jN=EH?;s!}"
    "_&!_r#P@(%#1k2Tn1nOx5L%%{X1}5l#l}ysZ~s7^EeIc~#RC#p*Lo68tg8<Abyr%e(7ut(3e{_cPvfO*X`_hKmPt$!09)jOkO8Jt"
    "lnZmqQNL<Rt%HN-PQU7yTczp(TDvs<%)1e*ObPrF0NSMqG9aaWHc)P0#^Aq{VpuPA7+C@=9sq@Mc~kmKnkAs~^qRfcK%*y>(FOIh"
    "JDl!MzgTuF{1Qix3AG7E>X%MxrL01L-`=U_@|lIr(98;ihPzfR9YrLF!6w4u%~s48GFG6v_T^3@{LQ)8uyM=oN;Qn%K7!mtXDawz"
    "wqj}sWt!LWC$AO73Sfxdnys`zh%q_Lty|S|qzNP2Z1=mNKo+ye313a_(ZI;K$is|wCiZj&e_7ELXpTRWHV)R_IEW_qLVp<eKtf{q"
    "Ii~0OU3ZvCyU?usaSx@TnX)(A?&$jmGC*?2zEltkj^ajyw*jLeZ`{eNL?C#b@_8cPwe<@5dPDV-%|pILIS>fD!Hx=0r4q5*yG=JE"
    "3TTXwGr8Qb#8+4gsEsw>yC|Met9=p8sg%70?M@fq8ep;sn}y=AGP%TsFKO#NFH_vbY&OHa<ldOT#EE9+=5x6Kkaww!?5N6?dv};^"
    "l&^_Z6~l3Xh$LR?CzaO(uSem;o%p`B-HIp{jY^5&+rs$Gz9$>w`aOy7(VtI6nxy%v$2o)cxD-U9*U0n70MK|NT!2JOmjRj31%`Ug"
    "jkgM#Y}WR#DxQkzB#M2T(KQ0H+R2hN(mZDtz{k;4r<k<Nirm$)o|vvFt6jM1D4e@bAgY+TTg7{_hrUwII63$QoHS;0Dgw(y&Lp(m"
    "*)C%|k#{SacPDd*`d4H|YqmQ4t{!Q3;A!ilSs8~9^lX-rO}E2YQosMYWJ%<AJV#<EXl*8_qSJgt3tk3T$WeBY2(ZkP?;88Ps>oaR"
    "xZKJg4B?vFU5X*M0hy{cq~fsbl_^Z;+|wo;ZBx9sNm+_EOMvQpb;1eeY5Y>oS}gQ{GD|_LGvwwQU>4S5cawd~XxDRenHAV}^YyKv"
    "HLd%qAFB6B-hHarQC(E84}~*u&4=Hbl`$T3D{10t<>~6ITh^9vN*t@|Cvx6RbEMOOH-$u*T}%16P4uIXqJqp4SQ=#LWCN(u+QzoJ"
    "?2w0}q`PeK+PA0|Ws9_mQJs38rnfsOm~NXd;or0wq0+QHYtAefBWhCac_66mkm~(WZWn4{Z)CNcc6%DqvAe(1d8oHgmL_6LCOA70"
    "nYvQ>rH=23_qotzWX`p63{%gJ-Ae(TIcHKRdX_d9R)@W*BW{g#1$(Z05^74V;QMxvv|?Kq0gG2$9DB_ytjeoOHXPK+*L1q?{N(F)"
    "2UdGKQW{eJ7{&K>bkrk-BbwHQ_5Kg279%q|E$#VN>`hVQZzfQEi1&C<_IOa%)j`<`CG<TUlpSiv{cQx_I6pV5%poohlpM~sW@$G^"
    "XR}@a_Eitm!Ou^-KR(;aTWQJ*nTKaTZE$$DB)rjhJz{>VGqla@>|3m<KegN7t!JfI?+E9eL{0qEvbV0AOALLQ6<D|%m0aa;e{O|J"
    "O9a&d34H@E-6fc|YiuVx>^+=ntFOvHWrbfl?iAK}8sRpwP&Qqa)l@G@@NLvwYAxy;dIIi9Z(C^FY4N6Qu4Mo_-H<Q04jrUoR*Crs"
    "s!q=jUSB|vCtn@Besyv2)8(sIFaGkwPd{9|dUcsH9|linz2T*%y@k=OV%zPopqXz1Ve^e$tpT81fwm<f4x!kEIe}`5U2HuvN32fP"
    "32uvVyTH-XixgcMWkG$Jefi!xV#%D6fX_V`$>iQRL$*iG$R62QV(qP?!YI<To@BKs%<$vj!k;AdA#me`G>#t%%(oJjqR{7caO}P("
    "BHg4}eC5P{+L6|aU!_>MjR1BN2Wwvr^PRhmd9!uUzSmuhSfN$$n&-ieO0DE`Ap0w|QdlR%U9&CM3M&WUY_+N=u|$e&KkN8!Fma4X"
    "qWJ~=+!n|U3k@0VwqAUyUtg?bowK?)dUwZx6~@u~Q|b%jwCcRGl-4%2aq>hCs5(w>iGX7@fS#%W5Efs78}gA)`mG6InBm%jJl*l`"
    "c*^xwlqXMiuVs0T)9$}_+F2_Bb%*@17SIXJQs&=9HK6i%wyg(LrdU@INCkgeEk9J2D(O|_9rfJd6dN)#Xzk0vq=C^s$_+^*Ws5n%"
    "S}7z{vum}o`xFWbJ@FY^ztV1QL^GD!cev&BbC-P?o7F;cupYiqH5-Kn2lZ?;rw*Z>O`5{#Df5}pYX_C?Rc%5oncWn&vGnyR7PhHi"
    "`4)w3YAN<m*v2wTx)iplg~_qD&9S!4UTWLqj~yOs+c>IiW9p8lQ`^RpXzX0wM*FaPtZ$QF@99+ErfUqeMTHx+jp3<QxUt8W+gG?z"
    "bPuM&%{$Cfi&H0)D8vaUBEhc^27xU_Zk(!}+v*Au$(=~VUB23A?y@4T>WS5VHhYO_y`gu)`Q?k3Km6s#pH8v@!_V(N;S#5HE3Bq}"
    "8?8WL<r5cpG)!)MuV^<BFM+2kUwQ4Fs8>$T8q3}o1FJy`9vB+oQUdP5*RSL^<M$*$M^%$lIWY*OMHlt)Dd+Wc@{*+C`N!Lm9Mi%&"
    "6IAFLLE_mHD_F90Uqy<~JH0%=ytuf?ZR5l*jIEoO76%QCW4x}tXuD%kB}uReSmtG|75fFM<ma%+)*PbKBVaZHjAEaA$nb~i5?prq"
    "In^)2v|*VQTGD6<4A7krMYhrZ5xEW+s0gl(cxuFxVzl_Rg59`%U2wZ+odkSibt-8Ck@ubVUq;JSKvEqk>?$^X#8$h5hC<%xU@J!2"
    "?^vzma-ZV7V#y?eI8V2NPLYpbdLS{2R*47Cf=9)OGuck?<CIN^{iN|*^XF8YH@GJK;kWb4mv))vh>j+B_SZJe^TgF-@(|L?%O8I{"
    "zpOh&HX^>jVcYus;_>Rr{TbZ{=F_M>UGp*q@oLl7l%7@SGG-9et#vAOBu;AAtT3tP)6`$pk^Pv?tcbV~-wTbXN#IIr(kM6GL9r!$"
    "#%cB4p(b7L^#9<W^Y`!1udbTO23E4_3|1$yxIPTQPsQ1Ly&Nv?r^gj#Ju{Tnz%5nyntggaM9@_N(;lQ>&k20eQ%`!QFprDyu%6Az"
    "gqUlomN~t&B&KkS5sBvp4HR)kjjAfKyp0wMa-Ps7iN$dW@tocQt!A=~^@3;=Gd%bfjPi!fK40|1o_<&Y{l}NDDsV2EPz4Yga4sK0"
    "rvW&z;zBVs^F-6G2m3~}qZQwb<>CaRo#jmmsa_E^*>PW4(6SM4Z(E&M^fV;dy-lC8IN>A0;ugAAJnO-;?bKp{bS(t8Bi#g>0`)30"
    "ww1S_Ww=|54&*2esXxjtZwl7UT{kKPWtTolT9s9T9U3{*KL3tF3ybd5FETT)1ehER<>47hRr;$1SzRPdo`yP_-F=(g)@C!8QR`y7"
    "`p#<TJy_ZHAXQ_}Ob5rNN`8XLjs^7@vG0d`k3Y%%)Hq@Vhip5nyw3@XFg2Ia%9{DyA`Ox@Wu`y6ilg_<t+CwiTbKHiSzefJk1X_~"
    "NAJ`p>*tS@-k*JU=+Fh{^;(-Mp_Fn-X4=!O(?OuG$d!nJVbD0ql~db6b{A#yrx1<Z3D9ywNTnlDBQ!<>QiZ=zu3eCyYrf4i&^;(t"
    "DcKH)j_&@PtYYXF57yo?AZzTN+~7mF!>T1ubdjOwOjeuBau_|1lwj=H<yK<sU8hFNbLm8+eLCV~uQ&mTjD$q&C@BfdSuwf7m9s}+"
    ">=_tZFm_z*;-+A84eV(<-3PG3n2WNkoOP$`kHXtfc$so8iO<5_z6F?u;F*sQ$vxbJ#Yb6olw}lRHdSPoOTw(@v@^K^aa7?vlJU(^"
    "p83FWyWDXbOh^HYgaB$#k&}~~s%Ut~C@U}Pk#U5zak3K}I+AyE!PpW;;NL|zG+Dx!XmbuSO%A1X@Y2#|^42<_mjY-4p*iEpP;QQ}"
    "Bgt)A1-VyQw;XqENjIv+=@d<>O+G<9J5qeH7J_Rh_WeLk!T$)l4ME2rY7wV*%(l8ou_fU6!&Ye15_giZ631K?3oP?2FTO|@7IvoQ"
    "4pzswbd0uBUIn4qa#$S%>&TO8)dQ!x?1?&Ad(lw!km@W_N9py1Ef|L9XjqQ==>YYU5>Rb&y-tYEqB?t{NA+_E>PLg7I@YU^o}+_A"
    "Y60<-On8LFWn?8R@lhO`bX}NnvyE}tBCX{jl<)c^l3Yjk1hjfRd6b=IDjz*+1c|G}ziL<&j{u1Seh3T2;dvJNtAxkL+|{Ow0wsxe"
    "=KOwA18OVD6U_|B$VTS}z#~;3_mCryjI1?-O5L=uf+?{!mQX&tRk}c-z)iM5$wZZqa}2oeTgExOTO)WYvmH3dN1^s6)J$!aJz3}C"
    "ES`x5wgrJK-+EkR-ps4aIOxE$%!{Y$WP47Et}g$vFL4-6yF2HcHc>lm4MEsbAr5g^ka4cZZFEESLakwMBLzq5;-0P_qu6(+CH6j%"
    ")d>5Yzy{kavp4<KQ7)7uNi<=?(H4i?4cug-J-{4A%~5~V?^;cup4q(`*j2hUV?yf%A#o+Q0r7IqwFOxEhH0+Ygop)qerBlsv7TIK"
    "cXPa_)TAl44$SLYZjXe62E#JiEWPB4C{vWR>sDs98UQblpNjO0rBfx1&<X=v3w)UNyN&1ti$^Jo3$kx1fZiiP<n5ae*OhnIvIS~7"
    "T=Mfi-u%9gOWF*Vs`>`r&f{=(Vp0~)!Rfk6MAqiXR)cCM5;fY21B(^haK(JhjomQ64TI7iRWPWc$Gw|ng5?=PAM+89rfzYk(f7H}"
    "&z_E{X2LGm?b?LyX9r1pPP?*T9|HOnuQS1u@)NQoV-_$j&W6G_2|OoD1FFl#jV$^e)WQ<_D)pw((E{(P#-lF3$hzMrBY#JABx-~U"
    "??_(WD;iBtoU2{<M)&r~z_f=p?iB{%B7s6Cag-#2WunQ9W7X9uWx3^Swce3Oz!oeYo?l*E3@a*Rgrz&hcmyBuDjuVv^`#7MH_F3s"
    "8ZWFR-NZxaMkjTZN75tq_~0||dMYjgRc^9B%&PUHB*a7JMawz&KGCP|g&aQV>VRtD*LD`a_FCq~q>;d?>|Z%oRKlsl_2c|a-GxMB"
    "3Q}=`UP4s%OI_yR2BqkqhqLsWxT49DKVz1{J6(nB3*H*x0}i0yG(JlsL9?IrL0i_|=N8&UbF6ADw!l##OsBV6s2kY}muDH8XDtdn"
    "<BiqodTQr-=DtdHx<f*ENt(Yuld46GqfuP<Ed-@+sn_$Td__IUaj4e`rmxQewXW_I6-q~z9L~u_ap0MEyISInnTdJ<@r`%PafnZ&"
    "p)BY!6qVDYwOB;4%Nwn*xjMxwD;wz`@r@XRJwe(C1dRcB@VV)J8pfb}`(@4bmy*-^@=L=`!CPnQ@;6RmE_Z7YtXw4|P<2gaMa8vz"
    "nKgke{2r_AqLlFe#0MkvhTHif`e4l*s}8|frU|H((~EPlrP^v^B|d6zDHKnLrOLFoxAME+=4>3WiZd~d8B%pu?&Z(QP`vnA+KHDx"
    "%Zm|*-VZ;kyJW0g-@4XhSB;Q4hU<~^0ZRAXRm@U1&a|<JTdf%Or=T~OmzQ<((j2a3c3y5UJA0Ty8{){mPA^0&r08;i9<gHpHE-_^"
    "VtL+Jna^A`D~L8+N4LnKG^LSM#rfQQ*}Noyw=q`y0fN$tN(Wk=!D3O3g%F+-B{28?vL#x&NhOeE<`MT=`r>kBJ}>U+(BV--z=J`W"
    "c;|21YZ4{11}y1YUg|Mq^hy&Rnha`!)kFw*l;erUR4V+w(1aH|;t_d~6^N-JozSCSL7kxJp5nwF?gf)|>ZB70pd%w5*+GCZ%X-`f"
    "3O!tPS_X-@<%0YdfkR$Vx(_tSmPg2OJ0z5lY6a6XF0RD&r9w}H9ENDJ-|Rsa{~j7H#C_Xh6#e=I>S9-`pfuOY()s*9{|{k#-n0"
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
    if not isinstance(value, list) or len(value) != 102:
        raise RuntimeError("pinned upstream tool contract must contain 102 tools")
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
