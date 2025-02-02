#include "config.h"
#include "cfirewall.h"
#include "dnx_nfq.h"

static void mangle_src_addr(struct dnx_pktb *pkt);
static void mangle_src_port(struct dnx_pktb *pkt);
static void mangle_dst_addr(struct dnx_pktb *pkt);
static void mangle_dst_port(struct dnx_pktb *pkt);


inline void
dnx_parse_nl_headers(nl_msg_hdr *nlmsgh, nl_pkt_hdr **nl_pkth,  struct nlattr **netlink_attrs, struct dnx_pktb *pkt)
{
    nfq_nlmsg_parse(nlmsgh, netlink_attrs);

    // in-int > src_zone | not available in POST ROUTE
    pkt->hw.iif = netlink_attrs[NFQA_IFINDEX_INDEV] ? ntohl(mnl_attr_get_u32(netlink_attrs[NFQA_IFINDEX_INDEV])) : 0;
    pkt->hw.in_zone = INTF_ZONE_MAP[pkt->hw.iif];

    // out-int > dst_zone | not available in PRE ROUTE
    pkt->hw.oif = netlink_attrs[NFQA_IFINDEX_OUTDEV] ? ntohl(mnl_attr_get_u32(netlink_attrs[NFQA_IFINDEX_OUTDEV])) : 0;
    pkt->hw.out_zone = INTF_ZONE_MAP[pkt->hw.oif];

    // standard mark
    //pkt->mark = netlink_attrs[NFQA_MARK] ? ntohl(mnl_attr_get_u32(netlink_attrs[NFQA_MARK])) : 0;

    // PACKET DATA / LEN
    pkt->data = mnl_attr_get_payload(netlink_attrs[NFQA_PAYLOAD]);
    pkt->tlen = mnl_attr_get_payload_len(netlink_attrs[NFQA_PAYLOAD]);

    *nl_pkth = (nl_pkt_hdr*) mnl_attr_get_payload(netlink_attrs[NFQA_PACKET_HDR]);
}

// initial header parse and assignment to dnx_pktb struct
inline void
dnx_parse_pkt_headers(struct dnx_pktb *pkt)
{
    /* ---------------------
       L3 - IP HEADER
    --------------------- */
    pkt->iphdr     = (struct IPhdr*) pkt->data;
    pkt->iphdr_len = (pkt->iphdr->ver_ihl & FOUR_BIT_MASK) * 4;

    /* ---------------------
       L4 - PROTOCOL HEADER
    --------------------- */
    // ICMP type/code will be contained in src port. dst port contain checksum.
    pkt->protohdr = (struct Protohdr*) (pkt->iphdr + 1);
}

// DIRECT ACTION (does NOT forward to another nfqueue)
inline void
dnx_send_verdict(struct cfdata *cfd, uint32_t pktid, uint32_t verdict)
{
    char                buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr    *nlh;
    struct nlattr      *nest;

    nlh = nfq_nlmsg_put(buf, NFQNL_MSG_VERDICT, cfd->queue);

    nfq_nlmsg_verdict_put(nlh, pktid, verdict);

    // connection offloaded to kernel if connmark is set. all connections will be offloaded until stateless actions are
    // implemented in cfirewall and configurable in the webui.
    nest = mnl_attr_nest_start(nlh, NFQA_CT);
    mnl_attr_put_u32(nlh, CTA_MARK, htonl(1));
    mnl_attr_nest_end(nlh, nest);

    mnl_socket_sendto(nl[cfd->idx], nlh, nlh->nlmsg_len);
}

/*
DEFERRED ACTION (forwarding to another nfqueue)
NO NAT (mangle) fast call.
sets verdict and mark.
reduced pointer dereference.
*/
inline void
dnx_send_deferred_verdict(struct cfdata *cfd, uint32_t pktid, uint32_t mark, uint32_t verdict)
{
    char                buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr    *nlh;
    struct nlattr      *nest;

    nlh = nfq_nlmsg_put(buf, NFQNL_MSG_VERDICT, cfd->queue);

    nfq_nlmsg_verdict_put(nlh, pktid, verdict);
    if (mark) {
        nfq_nlmsg_verdict_put_mark(nlh, mark);
    }

    // connection offloaded to kernel if connmark is set. all connections will be offloaded until stateless actions are
    // implemented in cfirewall and configurable in the webui.
    nest = mnl_attr_nest_start(nlh, NFQA_CT);
    mnl_attr_put_u32(nlh, CTA_MARK, htonl(1));
    mnl_attr_nest_end(nlh, nest);

    mnl_socket_sendto(nl[cfd->idx], nlh, nlh->nlmsg_len);
}

/*
DEFERRED ACTION (forwarding to another nfqueue)
PERFORMS NAT (mangle)
sets verdict and mark.
*/
//int
//dnx_send_deferred_verdict_with_mangle(struct cfdata *cfd, uint32_t pktid, struct dnx_pktb *pkt)
//{
//    char                buf[MNL_SOCKET_BUFFER_SIZE];
//    struct nlmsghdr    *nlh;
//
//    ssize_t     ret;
//
//    nlh = nfq_nlmsg_put(buf, NFQNL_MSG_VERDICT, cfd->queue);
//
//    nfq_nlmsg_verdict_put(nlh, pktid, pkt->verdict);
//    nfq_nlmsg_verdict_put_mark(nlh, pkt->mark);
//    if (pkt->mangled) {
//        nfq_nlmsg_verdict_put_pkt(nlh, pkt->data, pkt->tlen);
//    }
//
//    ret = mnl_socket_sendto(nl[cfd->idx], nlh, nlh->nlmsg_len);
//
//    return ret < 0 ? ERR : OK;
//}

/* currently it will be possible to configure a source port to be natted to. this will lead to source port collisions,
but iirc netfilter uses the conn tuple (hashed value) for uniqueness so it would only collide if overloading an existing
connections conn tuple. in this case i feel like netfilter would identify that when updating the conntrack entry and be
able to dynamically change the source port to one available.

source port manipulation will be expanded in the future to allow for much more customized values so having this so open
is important to not restrict feature growth. This will just need to be understood by the user that a source port should
not be specified under normal conditions, unless there is an explicit reason to do so.
*/
//bool
//dnx_mangle_pkt(struct dnx_pktb *pkt)
//{
//    if (pkt->verdict == DNX_MASQ) {
//        // need to set nat struct for masquerade or else conn tuple will not be updated.
//        pkt->nat.saddr = intf_masquerade(pkt->hw.oif);
//
//        // defer mangle until after conntrack tuple is changed. caller can use nat.saddr as reference.
//    }
//    else if (pkt->verdict == DNX_SRC_NAT || pkt->verdict == DNX_FULL_NAT) {
//        mangle_src_addr(pkt);
//        mangle_src_port(pkt);
//    }
//    else if (pkt->verdict == DNX_DST_NAT || pkt->verdict == DNX_FULL_NAT) {
//        mangle_dst_addr(pkt);
//        mangle_dst_port(pkt);
//    }
//    else { return false; }
//
//    // try without checksum
//    //pkt->iphdr->check = 0;
//    //pkt->iphdr->check = calc_checksum((const uint8_t*) pkt->iphdr, pkt->iphdr_len);
//
//    return true;
//}

/* these functions are to clean up mangling logic.
we need to check whether the nat values have been set by the nat rule before mangling the packet, otherwise we could
overwrite the packet field with a 0, making the packet malformed (invalid)
*/
static inline void
mangle_src_addr(struct dnx_pktb *pkt)
{
    if (pkt->nat.saddr)
        pkt->iphdr->saddr = pkt->nat.saddr;
}

static inline void
mangle_src_port(struct dnx_pktb *pkt)
{
    if (pkt->nat.sport)
        pkt->protohdr->sport = pkt->nat.sport;
}

static inline void
mangle_dst_addr(struct dnx_pktb *pkt)
{
    if (pkt->nat.daddr)
        pkt->iphdr->daddr = pkt->nat.daddr;
}

static inline void
mangle_dst_port(struct dnx_pktb *pkt)
{
    if (pkt->nat.dport)
        pkt->protohdr->dport = pkt->nat.dport;
}
