import numpy as np

from rtctools.optimization.collocated_integrated_optimization_problem import (
    CollocatedIntegratedOptimizationProblem,
)
from rtctools.optimization.goal_programming_mixin import GoalProgrammingMixin
from rtctools.optimization.goal_programming_mixin_base import Goal
from rtctools.optimization.linearized_order_goal_programming_mixin import (
    LinearizedOrderGoalProgrammingMixin,
)
from rtctools.util import run_optimization_problem

from rtctools_heat_network.esdl.esdl_mixin import ESDLMixin
from rtctools_heat_network.heat_mixin import HeatMixin


class RevenueGoal(Goal):
    priority = 1

    order = 1

    def __init__(self, state, price_profile, nominal):
        self.state = state

        self.price_profile = price_profile
        self.function_nominal = nominal

    def function(self, optimization_problem, ensemble_member):
        canonical, sign = optimization_problem.alias_relation.canonical_signed(self.state)
        symbols = sign * optimization_problem.state_vector(canonical, ensemble_member) * optimization_problem.variable_nominal(self.state)
        price_profile = optimization_problem.get_timeseries(self.price_profile).values
        sum = 0.
        for i in range(len(price_profile)):
            sum += symbols[i] * price_profile[i]

        return -sum


class _GoalsAndOptions:
    def goals(self):
        goals = super().goals().copy()

        for demand in self.heat_network_components["electricity_demand"]:
            price_profile = f"{demand}.electricity_price"
            state = f"{demand}.Electricity_demand"
            nominal = self.variable_nominal(state) * np.median(
                self.get_timeseries(price_profile).values)

            goals.append(RevenueGoal(state, price_profile, nominal))

        for demand in self.heat_network_components.get("gas_demand", []):
            price_profile = f"{demand}.gas_price"
            state = f"{demand}.Gas_demand_mass_flow"
            nominal=self.variable_nominal(state)*np.median(self.get_timeseries(price_profile).values)

            goals.append(RevenueGoal(state, price_profile, nominal))

        return goals

    def constraints(self, ensemble_member):
        constraints = super().constraints(ensemble_member)

        for gs in self.heat_network_components.get("gas_tank_storage", []):
            canonical, sign = self.alias_relation.canonical_signed(f"{gs}.Stored_gas_mass")
            storage_t0 = sign * self.state_vector(canonical, ensemble_member)[0]
            constraints.append((storage_t0, 0., 0.))
            canonical, sign = self.alias_relation.canonical_signed(f"{gs}.Gas_tank_flow")
            gas_flow_t0 = sign * self.state_vector(canonical, ensemble_member)[0]
            constraints.append((gas_flow_t0, 0., 0.))

        return constraints



class MILPProblem(
    _GoalsAndOptions,
    HeatMixin,
    LinearizedOrderGoalProgrammingMixin,
    GoalProgrammingMixin,
    ESDLMixin,
    CollocatedIntegratedOptimizationProblem,
):
    def path_goals(self):
        goals = super().path_goals().copy()

        return goals

    def solver_options(self):
        options = super().solver_options()
        options["solver"] = "highs"

        return options


if __name__ == "__main__":
    elect = run_optimization_problem(MILPProblem)
    r = elect.extract_results()
    print(r["Electrolyzer_fc66.ElectricityIn.Power"])
