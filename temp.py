from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, ipv6, arp
from ryu.lib import hub

from ryu.topology import event #, switches
from ryu.topology.api import get_all_host#, get_switch, get_link, get_all_switch
from operator import attrgetter

import networkx as nx
import time

class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        
        self.mac_to_port = {}
    
        self.switches = {}
        self.graph = nx.DiGraph()
        
        self.switch_delays = {}
        self.link_delays = {}

        self.hosts = {}

        self.port_features = {}
        self.free_bandwidth = {}
        self.port_stats = {}
        self.port_speed = {}

        self.monitor_time = 5

        # *** Running the _calc_delay() in a separate thread *** 
        self.discover_delays = hub.spawn(self._calc_delay)
        self.discover_delays = hub.spawn(self._calc_bandwidth)
        self.discover_hosts = hub.spawn(self._init_hosts)
        
    def _calc_delay(self):
        # *** Running get_delay_data() every 10s ***
        hub.sleep(10)
        # self.get_delay_data()
        # while True:
            # self.get_delay_data()
            # hub.sleep(10)

    def _calc_bandwidth(self):
        
        while True:
            hub.sleep(self.monitor_time)
            # print(self.port_speed)
            print(self.free_bandwidth)
            for u, node in self.graph.nodes(data=True):
                datapath = node['data']
                ofproto = datapath.ofproto
                parser = datapath.ofproto_parser

                req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
                datapath.send_msg(req)

    def _init_hosts(self):
        hub.sleep(10)
        print("Getting hosts....")
        hosts = get_all_host(self)
        for host in hosts:
            print(host.ipv4, host.port.name, host.port.dpid, host.port.port_no)
            self.hosts[host.mac] = {'dpid': host.port.dpid, 'port': host.port.port_no}
        print(self.hosts)
    
    def get_delay_data(self):
        print("Running get_delay_data() .....")
        for edge in self.graph.edges():
            datapath = self.graph.node[edge[0]]['data']
            src_dpid = edge[0]
            dst_dpid = edge[1]
            src_port = self.graph[edge[0]][edge[1]]['port']
            # print(datapath, src_dpid, dst_dpid, src_port)
            
            # *** Sending packet out to measure the delay between the link **  
            self.send_packet(dp=datapath, src=src_dpid, dst=dst_dpid, out_port=src_port)
    
    def _get_speed(self, now, pre, period):
        if period:
            return (now - pre) / (period)
        else:
            return 0

    def _get_free_bw(self, capacity, speed):
        # BW:Mbit/s
        return max(capacity / 10**3 - (speed * 8) / 10**6, 0)

    def _get_time(self, sec, nsec):
        return sec + nsec / (10 ** 9)

    def _get_period(self, n_sec, n_nsec, p_sec, p_nsec):
        return self._get_time(n_sec, n_nsec) - self._get_time(p_sec, p_nsec)

    def _build_packet_out(self, datapath, buffer_id, src_port, dst_port, data):
        actions = []
        if dst_port:
            actions.append(datapath.ofproto_parser.OFPActionOutput(dst_port))

        msg_data = None
        if buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            if data is None:
                return None
            msg_data = data

        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath, buffer_id=buffer_id,
            data=msg_data, in_port=src_port, actions=actions)
        return out

    def send_packet_out(self, datapath, buffer_id, src_port, dst_port, data):
        out = self._build_packet_out(datapath, buffer_id,
                                     src_port, dst_port, data)
        if out:
            datapath.send_msg(out)

    def send_packet(self, dp, src, dst, out_port):
        ethertype = 0x08fc
        e = ethernet.ethernet(src='00:00:00:00:00:0'+str(src), dst='00:00:00:00:00:0'+str(dst), ethertype=ethertype)            
        pkt = packet.Packet()
        pkt.add_protocol(e)
        pkt.add_protocol(time.time())
        pkt.serialize()

        ofproto = dp.ofproto
        parser = dp.ofproto_parser
        action = [parser.OFPActionOutput(port=out_port)]

        out = parser.OFPPacketOut(
            datapath=dp, buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=action, data=pkt.data)

        dp.send_msg(out)
        self.link_delays[(src,dst)] = time.time()
        # print("Packet out sent for (" + str(src) + "," + str(dst) +") via port " + str(out_port))

    def add_flow(self, datapath, hard_timeout, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]

        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                hard_timeout=hard_timeout,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        # install table-miss flow entry
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, 0, match, actions)
        
        req = parser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(req)
        self.switch_delays[dpid] = time.time()
        
        self.logger.info("Registered switch with dpid=%s", datapath.id)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        self.switch_delays[dpid] = time.time() - self.switch_delays[dpid]
        self.port_features[dpid] = {}
        for p in ev.msg.body:
            print("Port {}: curr_speed={}, max_speed={}, supported={}, ".format(p.port_no, p.curr_speed, p.max_speed, p.supported))
            self.port_features[dpid][p.port_no] = (p.config, p.state, p.curr_speed)

        # print("s" + str(dpid) + " to controller = " + str(self.switch_delays[dpid]))
    
    def _save_stats(self, _dict, key, value, length):
        if key not in _dict:
            _dict[key] = []
        _dict[key].append(value)

        if len(_dict[key]) > length:
            _dict[key].pop(0)

    def _save_freebandwidth(self, dpid, port_no, speed):
        # Calculate free bandwidth of port and save it.
        port_state = self.port_features.get(dpid).get(port_no)
        if port_state:
            capacity = port_state[2]
            # print("Getting free bw", capacity, speed)
            curr_bw = self._get_free_bw(capacity, speed)
            self.free_bandwidth[dpid].setdefault(port_no, None)
            self.free_bandwidth[dpid][port_no] = curr_bw
        else:
            self.logger.info("Fail in getting port state")

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
            Save port's stats info
            Calculate port's speed and save it.
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        # self.stats['port'][dpid] = body
        self.free_bandwidth.setdefault(dpid, {})

        for stat in sorted(body, key=attrgetter('port_no')):
            port_no = stat.port_no
            if port_no != ofproto_v1_3.OFPP_LOCAL:
                key = (dpid, port_no)
                value = (stat.tx_bytes, stat.rx_bytes, stat.rx_errors, stat.duration_sec, stat.duration_nsec)

                self._save_stats(self.port_stats, key, value, 5)

                # Get port speed.
                pre = 0
                period = self.monitor_time
                tmp = self.port_stats[key]
                if len(tmp) > 1:
                    pre = tmp[-2][0] + tmp[-2][1]
                    period = self._get_period(tmp[-1][3], tmp[-1][4],
                                              tmp[-2][3], tmp[-2][4])

                # print(self.port_stats[key][-1][0] + self.port_stats[key][-1][1], pre, period)
                speed = self._get_speed(
                    self.port_stats[key][-1][0] + self.port_stats[key][-1][1],
                    pre, period)

                # print(dpid, port_no, speed)

                self._save_stats(self.port_speed, key, speed, 5)
                self._save_freebandwidth(dpid, port_no, speed)
    
    @set_ev_cls(event.EventSwitchEnter)
    def get_switch_enter(self, ev):
        switch = ev.switch
        dp = switch.dp
        dpid = switch.dp.id
        self.graph.add_node(dpid, data=dp)
        print("Switch added with dpid=" + str(switch.dp.id))
        # print(list(self.graph.nodes(data=True)))

    @set_ev_cls(event.EventLinkAdd)
    def get_link_add(self, ev):
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no

        print("Link added: {" + str(src_dpid) + " -> " + str(dst_dpid) + " from port:" + str(src_port) + "}")
        
        self.graph.add_edge(src_dpid, dst_dpid, port=src_port)
        datapath = self.graph.node[src_dpid]['data']

        # self.send_packet(dp=datapath, src=src_dpid, dst=dst_dpid, out_port=src_port)

    def flood(self, msg):
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        out = self._build_packet_out(datapath, ofproto.OFP_NO_BUFFER,
                                     ofproto.OFPP_CONTROLLER,
                                     ofproto.OFPP_FLOOD, msg.data)
        datapath.send_msg(out)
        self.logger.debug("Flooding msg")

    def arp_forwarding(self, msg, src_ip, dst_ip, eth_pkt):
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        out_port = self.mac_to_port[datapath.id].get(eth_pkt.dst)
        print("Out port = ", out_port)
        if out_port is not None:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth_pkt.dst,
                                    eth_type=eth_pkt.ethertype)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, 0, 1, match, actions)
            self.send_packet_out(datapath, msg.buffer_id, in_port,
                                 out_port, msg.data)
            self.logger.debug("Reply ARP to knew host")
        else:
            self.flood(msg)

    def mac_learning(self, dpid, src_mac, in_port):
        self.mac_to_port.setdefault(dpid, {})
        if src_mac in self.mac_to_port[dpid]:
            if in_port != self.mac_to_port[dpid][src_mac]:
                return False
        else:
            self.mac_to_port[dpid][src_mac] = in_port
            return True

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):

        # If you hit this you might want to increase the "miss_send_length" of your switch
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        ip_pkt_6 = pkt.get_protocol(ipv6.ipv6)
        arp_pkt = pkt.get_protocol(arp.arp)

        print("Packet IN")

        if isinstance(ip_pkt_6, ipv6.ipv6):
            print("IPV6 processing")
            actions = []
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6)
            self.add_flow(datapath, 0, 1, match, actions)
            return
        
        if isinstance(arp_pkt, arp.arp):
            print("ARP processing")
            if self.mac_learning(dpid, eth.src, in_port) is False:
                self.logger.info("ARP packet enter in different ports")
                return
            
            self.arp_forwarding(msg, arp_pkt.src_ip, arp_pkt.dst_ip, eth)

        dst = eth.dst
        src = eth.src
        
        # if eth.ethertype == 0x08fc:
        #     src_dpid = int(src[-1:])
        #     dst_dpid = int(dst[-1:])

        #     delay_src = self.switch_delays[src_dpid]
        #     delay_dst = self.switch_delays[dst_dpid]

        #     self.link_delays[(src_dpid, dst_dpid)] = (time.time() - self.link_delays[(src_dpid, dst_dpid)] - delay_dst/2 + delay_src/2) * 1000
            
        #     self.graph[src_dpid][dst_dpid]['delay'] = self.link_delays[(src_dpid, dst_dpid)]
            
        #     # print("delay " + str(src_dpid) + " -> " + str(dst_dpid) + " = " + str(self.link_delays[(src_dpid, dst_dpid)]) + "s")
        #     # print(self.link_delays)
        #     return

        self.logger.info("Packet in %s %s %s %s %s", dpid, src, dst, in_port, eth.ethertype)

        if isinstance(ipv4_pkt, ipv4.ipv4):
            print("IPV4 Packet......")
            print(ipv4_pkt)
            host_info = self.hosts[dst]
            print("Host info", host_info)
            print("Path from : " + str(dpid) + " -> " + str(host_info['dpid']))
            if dpid == host_info['dpid']:
                print("Reached last switch")
                out_port = host_info['port']
            else:
                print("Finding shortest path")
                print(list(nx.all_simple_paths(self.graph,source=dpid,target=host_info['dpid'])))
                shortest_path = nx.shortest_path(self.graph,source=dpid,target=host_info['dpid'], weight='delay')
                out_port = self.graph[shortest_path[0]][shortest_path[1]]['port']
                print(shortest_path)

            print("Out_port = ", out_port)

            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data

            actions = [parser.OFPActionOutput(port=out_port)]

            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return 


        # self.mac_to_port[dpid][src] = in_port

        # if dst in self.mac_to_port[dpid]:
        #     out_port = self.mac_to_port[dpid][dst]
        # else:
        #     out_port = ofproto.OFPP_FLOOD

        # actions = [parser.OFPActionOutput(out_port)]

        # # install a flow to avoid packet_in next time
        # if out_port != ofproto.OFPP_FLOOD:
        #     match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
        #     # verify if we have a valid buffer_id, if yes avoid to send both
        #     # flow_mod & packet_out
        #     if msg.buffer_id != ofproto.OFP_NO_BUFFER:
        #         self.add_flow(datapath, 0, 1, match, actions, msg.buffer_id)
        #         return
        #     else:
        #         self.add_flow(datapath, 0, 1, match, actions)
        # data = None
        # if msg.buffer_id == ofproto.OFP_NO_BUFFER:
        #     data = msg.data

        # out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
        #                             in_port=in_port, actions=actions, data=data)
        # datapath.send_msg(out)
