from time import sleep, time
from typing import Tuple

from dobot_python.interface import Interface

# ────────────────────────────────────────────────────────────────────────────────
# High-level Dobot façade
# ────────────────────────────────────────────────────────────────────────────────

class Dobot:
    """Thin façade around :class:`~dobot_package.lib.interface.Interface` for PTP and suction."""

    #: PTP mode indices according to Communication-Protocol v1.1.5
    _MODE_MOVJ_XYZ = 1  # joint-space arc,      absolute pose
    _MODE_MOVL_XYZ = 2  # linear path,          absolute pose
    _MODE_MOVL_INC = 7  # linear path,          *incremental* pose

    # Partial lookup table: (byte, bit) → human-readable message
    _ALARM_MAP = {
        (0, 0): "Joint-1 limit reached",
        (0, 1): "Joint-2 limit reached",
        (0, 2): "Joint-3 limit reached",
        (0, 3): "Joint-4 limit reached",
        (0, 4): "Pose outside workspace",
        (1, 0): "Emergency stop pressed",
        (1, 2): "PTP timeout",
        (2, 0): "Vacuum timeout / no piece detected",
    }

    def __init__(self, port: str, vel: float = 50.0, acc: float = 50.0) -> None:
        """Open *port* and prime the controller."""
        self.interface = Interface(port)

        # Clean slate
        self.interface.stop_queue(True)
        self.interface.clear_queue()
        self.interface.start_queue()

        # Store & push motion params
        self._vel = vel
        self._acc = acc
        self.set_motion_params(vel, acc)

    def connected(self) -> bool:
        """Return True if the serial port is open and healthy."""
        return self.interface.connected()

    def get_pose(self) -> Tuple[float, float, float, float, float, float, float, float]:
        """Fetch the current pose reported by the controller as an 8-float tuple."""
        return self.interface.get_pose()

    def set_motion_params(self, vel: float, acc: float, *, queue: bool = False) -> None:
        """Update PTP joint velocity and acceleration for subsequent moves."""
        self._vel, self._acc = vel, acc
        self.interface.set_point_to_point_joint_params([vel] * 4, [acc] * 4, queue=queue)

    def set_ptp_coordinate_params(
        self,
        xyz_vel: float,
        r_vel: float,
        xyz_acc: float,
        r_acc: float,
        *,
        queue: bool = False,
    ) -> None:
        """Update PTP Cartesian and tool-axis speed settings."""
        self.interface.set_point_to_point_coordinate_params(
            xyz_vel,
            r_vel,
            xyz_acc,
            r_acc,
            queue=queue,
        )

    def set_ptp_common_params(self, vel_ratio: float, acc_ratio: float, *, queue: bool = False) -> None:
        """Update the shared PTP speed ratios."""
        self.interface.set_point_to_point_common_params(vel_ratio, acc_ratio, queue=queue)

    def _check_alarm(self) -> None:
        """Poll the alarm register and raise RuntimeError if any bits are set."""
        raw = self.interface.get_alarms_state()
        active = []
        for byte_idx, byte_val in enumerate(raw):
            for bit in range(8):
                if byte_val & (1 << bit):
                    active.append(self._ALARM_MAP.get((byte_idx, bit), f"Unknown alarm {byte_idx}.{bit}"))
        if active:
            raise RuntimeError("Dobot alarm – " + "; ".join(active))

    def clear_alarms(self) -> None:
        """Clear all alarm bits in the controller."""
        self.interface.clear_alarms_state()

    def wait(self, queue_index=None, timeout: float = 30.0):
        """Block until queue_index (defaults to the last one before WAIT) is done, then check alarms."""
        # 1) Capture the controller’s current index if none supplied
        if queue_index is None:
            queue_index = self.interface.get_current_queue_index()

        # 2) Enqueue a zero-delay WAIT so the controller bumps its index
        self.interface.wait(0, queue=True)

        # 3) Spin until that index has passed
        start = time()
        while self.interface.get_current_queue_index() <= queue_index:
            if timeout is not None and (time() - start) > timeout:
                raise TimeoutError(f"Dobot queue wait timed out at index {queue_index}")
            sleep(0.05)

        # 4) Finally check for any alarms
        self._check_alarm()


    def home(self, *, wait: bool = False, timeout: float = 60.0) -> None:
        """Return the arm to its zero (homed) pose."""
        queue_index = self.interface.set_homing_command(0)
        if wait:
            self.wait(queue_index, timeout=timeout)

    def move_joint(self, x: float, y: float, z: float, r: float, *, wait: bool = False) -> None:
        """MOVJ absolute – fastest joint-space PTP path."""
        
        self.interface.set_point_to_point_command(self._MODE_MOVJ_XYZ, x, y, z, r)
        if wait:
            self.wait()

    def move_linear(self, x: float, y: float, z: float, r: float, *, wait: bool = False) -> None:
        """MOVL absolute – straight-line tool tip path."""
        self.interface.set_point_to_point_command(self._MODE_MOVL_XYZ, x, y, z, r)
        if wait:
            self.wait()

    def move_linear_rel(self, dx: float, dy: float, dz: float, dr: float, *, wait: bool = False) -> None:
        """MOVL relative – incremental Cartesian PTP path."""
        self.interface.set_point_to_point_command(self._MODE_MOVL_INC, dx, dy, dz, dr)
        if wait:
            self.wait()

    def set_suction(self, on: bool, *, wait: bool = False) -> None:
        """Switch the suction cup on (True) or off (False)."""
        self.interface.set_end_effector_suction_cup(1, 1 if on else 0, queue=True)
        if wait:
            self.wait()
