"""
NodeMonitor — surveille les nœuds ROS2 définis dans un fichier YAML.

Stratégie de détection (double timer, sans callback natif) :
  1. Timer rapide (0.5s)  → compare le snapshot du graphe, déclenche les
                            réactions dès qu'un nœud apparaît ou disparaît.
  2. Timer lent  (poll_interval, 3s) → re-synchronise l'état complet,
                            filet de sécurité contre les faux positifs.

Le callback graph de rclpy n'est pas exposé en Python sur Jazzy/Humble ;
cette approche double-timer offre une latence équivalente (~0.5s) sans
dépendre d'une API non bindée.

Réactions configurables par nœud :
  - Publication sur /monitor/alerts          (std_msgs/String, toujours)
  - Publication sur /diagnostics             (diagnostic_msgs/DiagnosticArray)
    → compatible rqt_runtime_monitor out-of-the-box
  - Respawn via subprocess                   (si respawn: true + respawn_cmd)
  - Log ERROR / WARN selon criticité
"""

import subprocess
import threading
from pathlib import Path
from typing import Any

import rclpy
import yaml
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from rclpy.node import Node
from std_msgs.msg import String



class MonitoredNodeConfig:
    """Représente la config d'un nœud à surveiller."""

    def __init__(self, data: dict[str, Any]):
        self.name: str           = data['name']   
        self.namespace: str      = data.get('namespace', '/')
        self.required: bool      = data.get('required', True)
        self.respawn: bool       = data.get('respawn', False)
        self.respawn_cmd: str    = data.get('respawn_cmd', '')
        self.respawn_delay: float = data.get('respawn_delay', 2.0)
        self.description: str    = data.get('description', '')

        # État interne
        self._present: bool      = False
        self._respawn_pending: bool = False

    @property
    def full_name(self) -> str:
        """Nom complet avec namespace : /robot1/camera_driver."""
        if self.namespace == '/' or self.namespace == '':
            return self.name
        ns = self.namespace.rstrip('/')
        n  = self.name   if self.name.startswith('/')   else f'/{self.name}'
        return f'{ns}{n}'

    def __repr__(self) -> str:
        return f'<MonitoredNode {self.full_name} required={self.required}>'



class NodeMonitor(Node):

    DEFAULT_POLL_INTERVAL = 3.0   # secondes entre chaque polling
    DEFAULT_CONFIG_FILE   = ''    

    def __init__(self):
        super().__init__('node_monitor')

       
        self.declare_parameter('config_file',    self.DEFAULT_CONFIG_FILE)
        self.declare_parameter('poll_interval',  self.DEFAULT_POLL_INTERVAL)
        self.declare_parameter('dry_run',        False)   
        self.declare_parameter('diag_rate',      1.0)     

        config_file   = self.get_parameter('config_file').get_parameter_value().string_value
        poll_interval = self.get_parameter('poll_interval').get_parameter_value().double_value
        self._dry_run = self.get_parameter('dry_run').get_parameter_value().bool_value
        diag_rate     = self.get_parameter('diag_rate').get_parameter_value().double_value

        
        self._monitored: list[MonitoredNodeConfig] = []
        if config_file:
            self._load_config(config_file)
        else:
            self.get_logger().warn(
                'Paramètre config_file non fourni — aucun nœud surveillé.'
            )

        self._alert_pub = self.create_publisher(String, '/monitor/alerts', 10)

        # /diagnostics est le topic standard consommé par diagnostic_aggregator
        # et affiché directement par rqt_runtime_monitor.
        # Le hardware_id identifie ce monitor dans le panneau RQT.
        self._diag_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10)
        self._diag_timer = self.create_timer(1.0 / diag_rate, self._publish_diagnostics)

        # On garde le dernier snapshot connu pour détecter tout changement
        # dans le timer rapide, sans avoir besoin d'un callback natif.
        self._last_graph_snapshot: frozenset[str] = frozenset()

        # Lit uniquement get_node_names_and_namespaces() et déclenche
        # _poll_nodes() seulement si le graphe a effectivement changé.
        self.declare_parameter('fast_poll_interval', 0.5)
        fast_interval = self.get_parameter('fast_poll_interval').get_parameter_value().double_value
        self._fast_timer = self.create_timer(fast_interval, self._fast_check)

        # Vérifie l'état complet de tous les nœuds surveillés même si
        # aucun changement de graphe n'a été détecté (filet de sécurité).
        self._poll_timer = self.create_timer(poll_interval, self._poll_nodes)

        self.get_logger().info(
            f'NodeMonitor démarré | '
            f'détection={fast_interval}s | sync={poll_interval}s | '
            f'{len(self._monitored)} nœud(s) surveillé(s) | '
            f'dry_run={self._dry_run}'
        )

        self._lock = threading.Lock()


    def _load_config(self, path: str) -> None:
        """Charge et valide le fichier YAML de configuration."""
        p = Path(path)
        if not p.exists():
            self.get_logger().error(f'Config introuvable : {path}')
            return

        try:
            with p.open() as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            self.get_logger().error(f'Erreur parsing YAML : {exc}')
            return

        entries = data.get('monitored_nodes', [])
        if not entries:
            self.get_logger().warn('Aucune entrée monitored_nodes dans la config.')
            return

        for entry in entries:
            try:
                cfg = MonitoredNodeConfig(entry)
                self._monitored.append(cfg)
                self.get_logger().info(f'  + {cfg.full_name} (required={cfg.required})')
            except KeyError as e:
                self.get_logger().error(f'Entrée YAML invalide (champ manquant {e}) : {entry}')

        self.get_logger().info(f'{len(self._monitored)} nœud(s) chargé(s) depuis {path}')


    def _get_active_node_names(self) -> set[str]:
        """Retourne l'ensemble des noms de nœuds actifs (avec namespace)."""
        pairs = self.get_node_names_and_namespaces()
        active = set()
        for name, ns in pairs:
            ns = ns.rstrip('/')
            full = f'{ns}/{name}' if ns else f'/{name}'
            active.add(full)
        return active

    def _fast_check(self) -> None:
        """
        Timer rapide (0.5s) — détecte tout changement du graphe ROS.

        Compare le snapshot courant au précédent. Si identique : rien à faire
        (cas le plus fréquent, coût minimal). Si différent : déclenche
        immédiatement _poll_nodes() en lui passant le snapshot déjà calculé
        pour éviter un double appel à get_node_names_and_namespaces().
        """
        current = self._get_active_node_names()
        snapshot = frozenset(current)
        if snapshot != self._last_graph_snapshot:
            self._last_graph_snapshot = snapshot
            self._poll_nodes(active=current)

    def _poll_nodes(self, active: set[str] | None = None) -> None:
        """
        Compare la config vs le graphe réel et déclenche les réactions.

        active : snapshot déjà calculé (passé par _fast_check pour éviter
                 un double appel réseau). Si None, on interroge le graphe.
        """
        with self._lock:
            if active is None:
                active = self._get_active_node_names()
            for cfg in self._monitored:
                was_present = cfg._present
                is_present  = cfg.full_name in active

                if was_present and not is_present:
                    # → Nœud vient de disparaître
                    self._on_node_lost(cfg)
                elif not was_present and is_present:
                    # → Nœud vient d'apparaître (démarré / respawné)
                    self._on_node_recovered(cfg)

                cfg._present = is_present


    def _on_node_lost(self, cfg: MonitoredNodeConfig) -> None:
        """Un nœud surveillé a disparu du graphe."""
        level = 'CRITICAL' if cfg.required else 'WARNING'
        msg   = f'[{level}] Nœud perdu : {cfg.full_name}'
        if cfg.description:
            msg += f' ({cfg.description})'

        if cfg.required:
            self.get_logger().error(msg)
        else:
            self.get_logger().warn(msg)

        # Publication de l'alerte
        alert = String()
        alert.data = f'{level}|{cfg.full_name}'
        self._alert_pub.publish(alert)

        # Respawn si configuré et si ce n'est pas déjà en cours
        if cfg.respawn and cfg.respawn_cmd and not cfg._respawn_pending:
            if self._dry_run:
                self.get_logger().info(
                    f'[dry_run] Respawn simulé pour {cfg.full_name} '
                    f'dans {cfg.respawn_delay}s : {cfg.respawn_cmd}'
                )
            else:
                cfg._respawn_pending = True
                self.get_logger().info(
                    f'Respawn de {cfg.full_name} dans {cfg.respawn_delay}s...'
                )
                # Timer one-shot pour le délai de respawn
                self.create_timer(
                    cfg.respawn_delay,
                    lambda c=cfg: self._do_respawn(c)
                )

    def _on_node_recovered(self, cfg: MonitoredNodeConfig) -> None:
        """Un nœud surveillé est revenu sur le graphe."""
        cfg._respawn_pending = False
        self.get_logger().info(f'[OK] Nœud de retour : {cfg.full_name}')

        recovery = String()
        recovery.data = f'RECOVERED|{cfg.full_name}'
        self._alert_pub.publish(recovery)

    def _do_respawn(self, cfg: MonitoredNodeConfig) -> None:
        """Lance le processus de respawn via subprocess."""
        self.get_logger().info(f'Lancement : {cfg.respawn_cmd}')
        try:
            subprocess.Popen(
                cfg.respawn_cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.get_logger().error(
                f'Échec du respawn de {cfg.full_name} : {exc}'
            )
            cfg._respawn_pending = False


    def _publish_diagnostics(self) -> None:
        """
        Publie un DiagnosticArray sur /diagnostics.

        Mapping statut → niveau DiagnosticStatus :
          nœud présent + required     → OK    (0)
          nœud présent + non required → OK    (0)
          nœud absent  + non required → WARN  (1)
          nœud absent  + required     → ERROR (2)
          respawn en cours            → WARN  (1)  avec note
        """
        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()

        with self._lock:
            for cfg in self._monitored:
                status = DiagnosticStatus()

                # Nom affiché dans rqt_runtime_monitor : "node_monitor: /camera_driver"
                status.name      = f'node_monitor: {cfg.full_name}'
                status.hardware_id = 'node_monitor'

                if cfg._present:
                    status.level   = DiagnosticStatus.OK
                    status.message = 'Running'
                elif cfg._respawn_pending:
                    status.level   = DiagnosticStatus.WARN
                    status.message = f'Absent — respawn en cours (délai {cfg.respawn_delay}s)'
                elif cfg.required:
                    status.level   = DiagnosticStatus.ERROR
                    status.message = 'ABSENT — nœud requis manquant'
                else:
                    status.level   = DiagnosticStatus.WARN
                    status.message = 'Absent (non critique)'

                # Métadonnées visibles dans le panneau détail de RQT
                status.values = [
                    KeyValue(key='full_name',      value=cfg.full_name),
                    KeyValue(key='required',        value=str(cfg.required)),
                    KeyValue(key='respawn_enabled', value=str(cfg.respawn)),
                    KeyValue(key='description',     value=cfg.description or '—'),
                ]
                if cfg.respawn and cfg.respawn_cmd:
                    status.values.append(
                        KeyValue(key='respawn_cmd', value=cfg.respawn_cmd)
                    )

                array.status.append(status)

        self._diag_pub.publish(array)



def main(args=None):
    rclpy.init(args=args)
    node = NodeMonitor()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()