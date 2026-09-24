"""PiCar Freenove — Freenove 4WD Smart Car on a Raspberry Pi, over MCP.

The car runs `picar_freenove_fastapi` (github.com/pi-drg/picar_freenove_fastapi),
which exposes an HTTP control surface on the Pi itself. This plugin adapts that
surface to MCP tools; the gateway runs off-board and reaches the car over the
network.

Point it at your car with `PICAR_FREENOVE_URL` (default
`http://picar-freenove.local:8080`), and set `PICAR_FREENOVE_TOKEN` if the robot
is running with `ROBOT_TOKEN` configured.

Naming: the package directory must be an importable Python module, so it is
`picar_freenove` with an underscore, and `url_prefix` matches it because the
gateway mounts each robot at `/{plugin_name}/mcp` — a mismatch would make the
exported descriptor advertise an endpoint that 404s.
"""

import os
from core.plugin import RobotPlugin, RobotMetadata


class PicarFreenovePlugin(RobotPlugin):
    def metadata(self) -> RobotMetadata:
        return RobotMetadata(
            # Every operator's car is a different physical robot in a different
            # place, and this name is what goes on-chain at registration. Left
            # hardcoded, the only ways to register your own car are to publish
            # the reference car's name or to fork the plugin. The default keeps
            # existing deployments byte-identical.
            name=os.getenv("PICAR_FREENOVE_NAME", "PiCar-Finland-01"),
            description=(
                "A Freenove 4WD Smart Car on a Raspberry Pi: drive and strafe, "
                "pan/tilt camera with snapshots, ultrasonic distance and sweep, "
                "infrared line sensors, battery telemetry and an addressable "
                "LED ring."
            ),
            # Mecanum-equipped cars are holonomic, but the same plugin serves
            # cars with ordinary wheels, so the broader classification is the
            # honest one. picar_freenove_capabilities reports the actual build.
            robot_type="mobile_robot",
            url_prefix="picar_freenove",
            # Fleet identity is assigned by whoever registers the robot on-chain, not
            # claimed here — a gateway cannot verify whose fleet it belongs to. Left empty
            # so the descriptor carries no unverified claim; the registrar fills it in.
            fleet_provider="",
            fleet_domain="",
            # Not participating in the task marketplace. Enabling it means
            # setting BiddingTerms here and implementing bid()/execute() — both
            # are commercial decisions (what tasks, at what price), not
            # something to infer from the hardware.
            bidding_terms=None,
        )

    def tool_names(self) -> list[str]:
        return [
            "picar_freenove_is_online",
            "picar_freenove_capabilities",
            "picar_freenove_battery",
            "picar_freenove_drive",
            "picar_freenove_move",
            "picar_freenove_stop",
            "picar_freenove_look",
            "picar_freenove_distance",
            "picar_freenove_scan",
            "picar_freenove_line",
            "picar_freenove_snapshot",
            "picar_freenove_led",
        ]

    def control_base_urls(self) -> list[str]:
        """The car's own FastAPI server — the gateway proxies /ws/* to it.

        Same candidates the MCP adapter resolves from, deliberately: both must
        agree on which car they are talking to.
        """
        from .robot_adapter import control_base_urls

        return control_base_urls()

    def control_auth_token(self) -> str | None:
        """PICAR_FREENOVE_TOKEN, or None when the car runs with auth disabled."""
        from .robot_adapter import control_token

        return control_token() or None

    def register_tools(self, mcp):
        from .robot_adapter import PicarFreenoveAdapter
        from .mcp_tools import register

        self.adapter = PicarFreenoveAdapter()
        register(mcp, self.adapter)
