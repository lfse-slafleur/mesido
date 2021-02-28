import math
from typing import Dict, Tuple, Type

from rtctools_heat_network.pycml.component_library.heat import (
    Buffer,
    Demand,
    Node,
    Pipe,
    Pump,
    Source,
)

from . import esdl
from .asset_to_component_base import MODIFIERS, _AssetToComponentBase
from .common import Asset
from .esdl_model_base import _ESDLModelBase


class AssetToHeatComponent(_AssetToComponentBase):
    def __init__(self, *args, v_nominal=1.0, v_max=5.0, rho=988.0, cp=4200.0, **kwargs):
        super().__init__(*args, **kwargs)

        self.v_nominal = v_nominal
        self.v_max = v_max
        self.rho = rho
        self.cp = cp

    @property
    def _rho_cp_modifiers(self):
        return dict(rho=self.rho, cp=self.cp)

    def convert_buffer(self, asset: Asset) -> Tuple[Type[Buffer], MODIFIERS]:
        assert asset.asset_type == "HeatStorage"

        max_heat = asset.attributes["capacity"]
        assert max_heat > 0.0

        modifiers = dict(
            Q_nominal=self._get_connected_q_nominal(asset),
            Stored_heat=dict(min=0.0, max=max_heat),
            init_Heat=0.0,
            **self._rho_cp_modifiers,
        )

        return Buffer, modifiers

    def convert_demand(self, asset: Asset) -> Tuple[Type[Demand], MODIFIERS]:
        assert asset.asset_type in {"GenericConsumer", "HeatingDemand"}

        max_demand = asset.attributes["power"]
        assert max_demand > 0.0

        modifiers = dict(
            Q_nominal=self._get_connected_q_nominal(asset),
            Heat_demand=dict(max=max_demand),
            **self._supply_return_temperature_modifiers(asset),
            **self._rho_cp_modifiers,
        )

        return Demand, modifiers

    def convert_node(self, asset: Asset) -> Tuple[Type[Node], MODIFIERS]:
        assert asset.asset_type == "Joint"

        sum_in = 0
        sum_out = 0

        for x in asset.attributes["port"].items:
            if type(x) == esdl.esdl.InPort:
                sum_in += len(x.connectedTo)
            if type(x) == esdl.esdl.OutPort:
                sum_out += len(x.connectedTo)

        modifiers = dict(
            n=sum_in + sum_out,
        )

        return Node, modifiers

    def convert_pipe(self, asset: Asset) -> Tuple[Type[Pipe], MODIFIERS]:
        assert asset.asset_type == "Pipe"

        supply_temperature, return_temperature = self._get_supply_return_temperatures(asset)

        if "_ret" in asset.attributes["name"]:
            temperature = return_temperature
        else:
            temperature = supply_temperature

        # Compute the maximum heat flow based on an assumed maximum velocity
        diameter = asset.attributes["innerDiameter"]
        area = math.pi * asset.attributes["innerDiameter"] ** 2 / 4.0
        q_max = area * self.v_max
        q_nominal = area * self.v_nominal

        self._set_q_nominal(asset, q_nominal)

        # TODO: This might be an underestimation. We need to add the total
        # heat losses in the system to get a proper upper bound. Maybe move
        # calculation of Heat bounds to the HeatMixin?
        delta_temperature = supply_temperature - return_temperature
        hfr_nominal = self.rho * self.cp * q_nominal * delta_temperature
        hfr_max = self.rho * self.cp * q_max * delta_temperature * 2

        assert hfr_max > 0.0

        # Insulation properties
        material = asset.attributes["material"]
        # NaN means the default values will be used
        insulation_thicknesses = math.nan
        conductivies_insulation = math.nan

        if material is not None:
            assert isinstance(material, esdl.esdl.CompoundMatter)
            components = material.component.items
            if components:
                insulation_thicknesses = [x.layerWidth for x in components]
                conductivies_insulation = [x.matter.thermalConductivity for x in components]

        modifiers = dict(
            Q_nominal=q_nominal,
            length=asset.attributes["length"],
            diameter=diameter,
            temperature=temperature,
            HeatIn=dict(
                Heat=dict(min=-hfr_max, max=hfr_max, nominal=hfr_nominal),
                Q=dict(min=-q_max, max=q_max),
            ),
            HeatOut=dict(
                Heat=dict(min=-hfr_max, max=hfr_max, nominal=hfr_nominal),
                Q=dict(min=-q_max, max=q_max),
            ),
            insulation_thickness=insulation_thicknesses,
            conductivity_insulation=conductivies_insulation,
            **self._supply_return_temperature_modifiers(asset),
            **self._rho_cp_modifiers,
        )

        return Pipe, modifiers

    def convert_pump(self, asset: Asset) -> Tuple[Type[Pump], MODIFIERS]:
        assert asset.asset_type == "Pump"

        modifiers = dict(
            Q_nominal=self._get_connected_q_nominal(asset),
            **self._supply_return_temperature_modifiers(asset),
            **self._rho_cp_modifiers,
        )

        return Pump, modifiers

    def convert_source(self, asset: Asset) -> Tuple[Type[Source], MODIFIERS]:
        assert asset.asset_type in {
            "GasHeater",
            "GenericProducer",
            "GeothermalSource",
            "ResidualHeatSource",
        }

        max_supply = asset.attributes["power"]
        assert max_supply > 0.0

        modifiers = dict(
            Q_nominal=self._get_connected_q_nominal(asset),
            Heat_source=dict(min=0.0, max=max_supply, nominal=max_supply / 2.0),
            **self._supply_return_temperature_modifiers(asset),
            **self._rho_cp_modifiers,
        )

        return Source, modifiers


class ESDLHeatModel(_ESDLModelBase):
    def __init__(self, assets: Dict[str, Asset], converter_class=AssetToHeatComponent, **kwargs):
        super().__init__(None)

        converter = converter_class(**kwargs)

        self._esdl_convert(converter, assets, "Heat")