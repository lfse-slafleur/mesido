import logging
from abc import abstractmethod
from enum import IntEnum
from typing import List, Optional, Tuple, Type, Union

import casadi as ca

import numpy as np

from rtctools.optimization.goal_programming_mixin_base import Goal
from rtctools.optimization.optimization_problem import BT

import rtctools_heat_network._darcy_weisbach as darcy_weisbach

from .constants import GRAVITATIONAL_CONSTANT
from .network_common import NetworkSettings
from .pipe_class import PipeClass


logger = logging.getLogger("rtctools_heat_network")


class HeadLossOption(IntEnum):
    r"""
    Enumeration for the possible options to take head loss in pipes into account.
    Also see :py:meth:`._HeadLossMixin.heat_network_options` for related options.

    .. note::
        Not all options are supported by :py:class:`.HeatMixin`, due to the focus
        on MILP formulations.

    NO_HEADLOSS
       The NO_HEADLOSS option assumes that there is no headloss in the pipelines.
       There are no constraints added relating the discharge to the head.

    CQ2_INEQUALITY
        As the name implies, this adds a quadratic inquality constraint between
        the head and the discharge in a pipe:

         .. math::

           dH \ge C \cdot Q^2

        This expression of the headloss requires a system-specific estimation of
        the constant C.

        As dH is always positive, a boolean is needed when flow directions are not
        fixed in a mixed-integer formulation to determine if

         .. math::

           dH = H_{up} - H_{down}

        or (when the :math:`Q < 0`)

         .. math::

           dH = H_{down} - H_{up}

    LINEARIZED_DW
        Just like ``CQ2_INEQUALITY``, this option adds inequality constraints:

        .. math::
           \Delta H \ge \vec{a} \cdot Q + \vec{b}

        with :math:`\vec{a}` and :math:`\vec{b}` the linearization coefficients.

        This approach can more easily be explain with a plot, showing the Darcy-Weisbach
        head loss, and the linear lines approximating it. Note that the number of
        supporting lines is an option that can be set by the user by overriding
        :py:meth:`._HeadLossMixin.heat_network_options`. Also note that, just like
        ``CQ2_INEQUALITY``, a boolean is needed when flow directions are not fixed.

           .. image:: /images/DWlinearization.PNG

    LINEAR
        This option uses a linear head loss formulation.
        A single constraint of the type

         .. math::

           H_{up} - H_{down} = dH = C \cdot Q

        is added.
        Note that no boolean are required to support the case where flow directions
        are not fixed yet, at the cost of reduced fidelity in the head-loss relationship.

        The exact velocity to use to linearize can be set by overriding
        :py:meth:`._HeadLossMixin.heat_network_options`.

    CQ2_EQUALITY
        This option adds **equality** constraints of the type:

         .. math::

           dH = C \cdot Q^2

        This equation is non-convex, and can therefore lead to convergence issues.
    """

    NO_HEADLOSS = 1
    CQ2_INEQUALITY = 2
    LINEARIZED_DW = 3  # LINEARIZED_N_LINES_WEAK_INEQUALITY
    LINEAR = 4  # LINEARIZED_ONE_LINE_EQUALITY
    CQ2_EQUALITY = 5
    LINEARIZED_N_LINES_EQUALITY = 6


class _MinimizeHeadLosses(Goal):
    order = 1

    priority = 2**31 - 1

    def __init__(self, optimization_problem, input_network_settings, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.optimization_problem = optimization_problem
        self.network_settings = input_network_settings
        self.function_nominal = len(optimization_problem.times())

    def function(self, optimization_problem, ensemble_member):
        """
        This function returns the summed head loss of all pipes and pumps.
        """
        sum_ = 0.0

        parameters = optimization_problem.parameters(ensemble_member)

        pumps = optimization_problem.heat_network_components.get("pump", [])
        sources = optimization_problem.heat_network_components.get("source", [])

        for p in pumps:
            sum_ += optimization_problem.state(f"{p}.dH")

        # If sources have an accompanying pump, we prefer the produced head to
        # be shifted to that pump. We therefore penalize the head of the
        # sources twice as much.
        for s in sources:
            sum_ += 2 * optimization_problem.state(f"{s}.dH")

        assert self.network_settings["head_loss_option"] != HeadLossOption.NO_HEADLOSS

        if self.network_settings["network_type"] == NetworkSettings.NETWORK_TYPE_HEAT:
            for p in optimization_problem.heat_network_components.get("pipe", []):
                if (
                    not parameters[f"{p}.has_control_valve"]
                    and not parameters[f"{p}.length"] == 0.0
                ):
                    sym_name = optimization_problem._hn_pipe_to_head_loss_map[p]
                    sum_ += optimization_problem.state(sym_name)
        elif self.network_settings["network_type"] == NetworkSettings.NETWORK_TYPE_GAS:
            for p in optimization_problem.heat_network_components.get("gas_pipe", []):
                if not parameters[f"{p}.length"] == 0.0:
                    sym_name = optimization_problem._hn_gas_pipe_to_head_loss_map[p]
                    sum_ += optimization_problem.state(sym_name)

        return sum_


class _MinimizeHydraulicPower(Goal):
    order = 1

    priority = 2**31 - 1

    def __init__(
        self,
        optimization_problem,
        input_network_settings,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.optimization_problem = optimization_problem
        self.network_settings = input_network_settings

    def function(self, optimization_problem, ensemble_member):
        """
        This function returns the summed hydraulic power of all pipes
        """
        sum_ = 0.0

        parameters = optimization_problem.parameters(ensemble_member)

        assert self.network_settings["head_loss_option"] != HeadLossOption.NO_HEADLOSS

        for pipe in optimization_problem.heat_network_components.get("pipe", []):
            if (
                not parameters[f"{pipe}.has_control_valve"]
                and not parameters[f"{pipe}.length"] == 0.0
            ):
                sum_ += optimization_problem.state(f"{pipe}.Hydraulic_power")

        return sum_


class HeadLossClass:
    """
    For handling of discharge - head (loss) relationship to the model.
    """

    def __init__(self, input_network_settings, *args, **kwargs):

        self.__pipe_head_bounds = {}

        self.__pipe_head_loss_var = {}
        self.__pipe_head_loss_bounds = {}
        self.__pipe_head_loss_nominals = {}
        self.__pipe_head_loss_zero_bounds = {}
        self._hn_pipe_to_head_loss_map = {}

        # Kvr
        # Boolean variables for the linear line segment options per pipe.
        self.__pipe_linear_line_segment_var = {}  # value 0/1: line segment - not active/active
        self.__pipe_linear_line_segment_var_bounds = {}
        self._pipe_linear_line_segment_map = {}

        self.__priority = None

        self.network_settings = input_network_settings

    def pre(self):
        """
        Some checks to avoid that different pipes have different head_loss options in case one
        has the No_HeadLoss option.
        """
        super().pre()

        self.__initialize_nominals_and_bounds()

        parameters = self.parameters(0)

        # It is not allowed to mix NO_HEADLOSS with other head loss options as
        # that just leads to weird and undefined behavior.
        head_loss_values = {
            self.network_settings["head_loss_option"],
        }

        pipe_type = "pipe"
        if len(self.heat_network_components.get("pipe", [])) == 0:
            pipe_type = "gas_pipe"

        for p in self.heat_network_components.get(pipe_type, []):
            head_loss_values.add(
                self._hn_get_pipe_head_loss_option(p, self.network_settings, parameters)
            )

        if HeadLossOption.NO_HEADLOSS in head_loss_values and len(head_loss_values) > 1:
            raise Exception(
                "Mixing .NO_HEADLOSS with other head loss options is not allowed. "
                "Either all pipes should have .NO_HEADLOSS set, or none. "
                "The global value returned by heat_network_options() also need to match."
            )

    def head_loss_network_options(self):
        r"""
        Returns a dictionary of heat network specific options.

        +--------------------------------+-----------+-----------------------------------+
        | Option                         | Type      | Default value                     |
        +================================+===========+===================================+
        | ``minimum_pressure_far_point`` | ``float`` | ``1.0`` bar                       |
        +--------------------------------+-----------+-----------------------------------+
        | ``wall_roughness``             | ``float`` | ``0.0002`` m                      |
        +--------------------------------+-----------+-----------------------------------+
        | ``estimated_velocity``         | ``float`` | ``1.0`` m/s (CQ2_* & LINEAR)      |
        +--------------------------------+-----------+-----------------------------------+

        The ``minimum_pressure_far_point`` gives the minimum pressure
        requirement at any demand node, which means that the pressure at the
        furthest point is also satisfied without inspecting the topology.

        The ``wall_roughness`` of the pipes plays a role in determining the
        resistance of the pipes.
        """

        options = {}

        options["minimum_pressure_far_point"] = 1.0
        options["wall_roughness"] = 2e-4
        options["estimated_velocity"] = 1.0

        return options

    @abstractmethod
    def _hn_get_pipe_head_loss_option(
        self, pipe, network_settings, parameters, **kwargs
    ) -> HeadLossOption:
        """
        The global user head loss option is not necessarily the same as the
        head loss option for a specific pipe. For example, when a control
        valve is present, a .LINEAR global head loss option could mean a
        .CQ2_INEQUALITY formulation should be used instead.

        See also the explanation of `head_loss_option` (and its values) in
        :py:meth:`.heat_network_options`.
        """
        raise NotImplementedError

    @abstractmethod
    def _hn_pipe_head_loss_constraints(self, ensemble_member) -> List[Tuple[ca.MX, float, float]]:
        """
        This method should be implemented to relate the three variables:

        - discharge: e.g. `pipe.Q`
        - head difference: e.g. `pipe.dH`
        - head loss of the pipe (note: proxy symbol that is >= abs(actual head loss))

        The internal variable name/symbol for the head loss can be retried via
        `self._hn_pipe_to_head_loss_map[pipe]`. The method that should/can be called to
        relate these three variables is :py:meth:`._hn_pipe_head_loss`.
        """
        raise NotImplementedError

    @property
    def _hn_minimization_goal_class(self) -> Type[Goal]:
        """
        This function returns the Minimize Head loss goal
        """
        return _MinimizeHeadLosses

    @property
    def _hpwr_minimization_goal_class(self) -> Type[Goal]:
        """
        This function returns the minimize hydraulic power goal
        """
        return _MinimizeHydraulicPower

    def initialize_variables_nominals_and_bounds(
        self, optimization_problem, commodity_type, pipe_name, network_settings
    ):
        """
        This function computes and sets the bounds and nominals for the head loss of all the pipes
        as well as the minimum and maximum pipe pressure.
        """
        options = optimization_problem.heat_network_options()
        parameters = optimization_problem.parameters(0)

        min_pressure = network_settings["pipe_minimum_pressure"]
        max_pressure = network_settings["pipe_maximum_pressure"]
        assert (
            max_pressure > min_pressure
        ), "The global maximum pressure must be larger than the minimum one."
        if np.isfinite(min_pressure) or np.isfinite(max_pressure):
            # No elevation data available yet. Assume 0 mDAT for now.
            pipe_elevation = 0.0
            min_head = min_pressure * 10.2 + pipe_elevation
            max_head = max_pressure * 10.2 + pipe_elevation
            self.__pipe_head_bounds[f"{pipe_name}.{commodity_type}In.H"] = (min_head, max_head)
            self.__pipe_head_bounds[f"{pipe_name}.{commodity_type}Out.H"] = (min_head, max_head)

        head_loss_option = network_settings["head_loss_option"]
        if head_loss_option not in HeadLossOption.__members__.values():
            raise Exception(f"Head loss option '{head_loss_option}' does not exist")

        length = parameters[f"{pipe_name}.length"]
        if length < 0.0:
            raise ValueError("Pipe length has to be larger than or equal to zero")

        if head_loss_option == HeadLossOption.NO_HEADLOSS or (
            length == 0.0 and not parameters[f"{pipe_name}.has_control_valve"]
        ):
            self.__pipe_head_loss_zero_bounds[f"{pipe_name}.dH"] = (0.0, 0.0)
            head_loss_var = ""  # Required for checking if the keys exist in the return past below
        else:
            q_nominal = self._hn_pipe_nominal_discharge(options, parameters, pipe_name)
            head_loss_nominal = self._hn_pipe_head_loss(
                pipe_name, optimization_problem, options, network_settings, parameters, q_nominal
            )

            self.__pipe_head_loss_nominals[f"{pipe_name}.dH"] = head_loss_nominal

            # The .dH is by definition "Out - In". The .__head_loss is by
            # definition larger than or equal to the absolute value of dH.
            head_loss_var = f"{pipe_name}.__head_loss"

            self._hn_pipe_to_head_loss_map[pipe_name] = head_loss_var
            self.__pipe_head_loss_var[head_loss_var] = ca.MX.sym(head_loss_var)

            self.__pipe_head_loss_nominals[head_loss_var] = head_loss_nominal
            self.__pipe_head_loss_bounds[head_loss_var] = (0.0, np.inf)

            if head_loss_option == HeadLossOption.LINEARIZED_N_LINES_EQUALITY:
                # Add pipe head loss linear line segment 
                self._pipe_linear_line_segment_map[pipe_name] = {}
                self.__pipe_linear_line_segment_var[pipe_name] = {}
                self.__pipe_linear_line_segment_var_bounds[pipe_name] = {}
                # We need to creat linear line segments for the + and - volumetric flow rate
                # possibilites
                discharge_type = ["neg_discharge", "pos_discharge"]
                line_number = 0
                for dtype in discharge_type:
                    for ii_line in range(network_settings["n_linearization_lines"] * 2):
                        if ii_line < network_settings["n_linearization_lines"]:
                            dtype = discharge_type[0]
                            line_number = ii_line + 1
                        else:
                            dtype = discharge_type[1]
                            line_number = ii_line + 1 - network_settings["n_linearization_lines"]

                        pipe_linear_line_segment_var_name = (
                            f"{pipe_name}__pipe_linear_line_segment_num_{line_number}_{dtype}"
                        )  # start line segment numbering from 1 up to "n_linearization_lines"

                        self._pipe_linear_line_segment_map[pipe_name][
                            ii_line
                        ] = pipe_linear_line_segment_var_name
                        self.__pipe_linear_line_segment_var[pipe_name][pipe_linear_line_segment_var_name] = (
                            ca.MX.sym(pipe_linear_line_segment_var_name)
                        )
                        self.__pipe_linear_line_segment_var_bounds[pipe_name][
                            pipe_linear_line_segment_var_name
                        ] = (0.0, 1.0)
                
        return (
            (
                self.__pipe_head_bounds[f"{pipe_name}.{commodity_type}In.H"]
                if self.__pipe_head_bounds.get(f"{pipe_name}.{commodity_type}In.H") is not None
                else self.__pipe_head_bounds
            ),
            (
                self.__pipe_head_bounds[f"{pipe_name}.{commodity_type}Out.H"]
                if self.__pipe_head_bounds.get(f"{pipe_name}.{commodity_type}Out.H") is not None
                else self.__pipe_head_bounds
            ),
            (
                self.__pipe_head_loss_zero_bounds[f"{pipe_name}.dH"]
                if self.__pipe_head_loss_zero_bounds.get(f"{pipe_name}.dH") is not None
                else self.__pipe_head_loss_zero_bounds
            ),
            (
                self._hn_pipe_to_head_loss_map[pipe_name]
                if self._hn_pipe_to_head_loss_map.get(pipe_name) is not None
                else self._hn_pipe_to_head_loss_map
            ),
            (
                self.__pipe_head_loss_var[head_loss_var]
                if self.__pipe_head_loss_var.get(head_loss_var) is not None
                else self.__pipe_head_loss_var
            ),
            (
                self.__pipe_head_loss_nominals[f"{pipe_name}.dH"]
                if self.__pipe_head_loss_nominals.get(f"{pipe_name}.dH") is not None
                else self.__pipe_head_loss_nominals
            ),
            (
                self.__pipe_head_loss_nominals[head_loss_var]
                if self.__pipe_head_loss_nominals.get(head_loss_var) is not None
                else self.__pipe_head_loss_nominals
            ),
            (
                self.__pipe_head_loss_bounds[head_loss_var]
                if self.__pipe_head_loss_bounds.get(head_loss_var) is not None
                else self.__pipe_head_loss_bounds
            ),
            (
                self._pipe_linear_line_segment_map[pipe_name]
                if self._pipe_linear_line_segment_map.get(pipe_name) is not None
                else self._pipe_linear_line_segment_map
            ),
            (
                self.__pipe_linear_line_segment_var[pipe_name]
                if self.__pipe_linear_line_segment_var.get(pipe_name) is not None
                else self.__pipe_linear_line_segment_var
            ),
            (
                self.__pipe_linear_line_segment_var_bounds[pipe_name]
                if self.__pipe_linear_line_segment_var_bounds.get(pipe_name) is not None
                else self.__pipe_linear_line_segment_var_bounds
            ),
        )

    def _hn_pipe_nominal_discharge(self, heat_network_options, parameters, pipe: str) -> float:
        """
        This function returns the nominal volumetric flow (m^3/s) through the pipe.
        """
        return parameters[f"{pipe}.area"] * heat_network_options["estimated_velocity"]

    def _hn_pipe_head_loss(
        self,
        pipe: str,
        optimization_problem,
        heat_network_options,
        network_settings,
        parameters,
        discharge: Union[ca.MX, float, np.ndarray],
        head_loss: Optional[ca.MX] = None,
        dh: Optional[ca.MX] = None,
        is_disconnected: Union[ca.MX, int] = 0,
        big_m: Optional[float] = None,
        pipe_class: Optional[PipeClass] = None,
    ) -> Union[List[Tuple[ca.MX, BT, BT]], float, np.ndarray]:
        """
        This function has two purposes:
        - return the head loss constraint expression(s) or
        - compute the head loss numerically (always positive).

        Note that there are different head loss formulations (see
        :class:`HeadLossOption`). Some formulations require the passing of
        `head_loss` (a symbol that is always positive by definition), and
        others require the passing of `dh` (which is negative when the flow is
        positive).

        When `head_loss` or `dh` is its corresponding MX symbol/expression,
        the appropriate constraint expression is returned. When `head_loss`
        and `dh` are both None, the `discharge` is assumed numerical, and the
        numerical computation of the appropriate head loss formulation is
        returned. Note that this returned numerical value is always positive,
        regardless of the sign of the discharge.

        `is_disconnected` can be used to specify whether a pipe is
        disconnected or not. This is most useful if a (boolean) ca.MX symbol
        is passed, which can then be used with a big-M formulation. The big-M
        itself then also needs to be passed via the `big_m` keyword argument.
        """

        if head_loss is None and dh is None:
            symbolic = False
        else:
            symbolic = True

        head_loss_option = network_settings["head_loss_option"]
        assert (
            head_loss_option != HeadLossOption.NO_HEADLOSS
        ), "This method should be skipped when NO_HEADLOSS is set."

        length = parameters[f"{pipe}.length"]

        if length == 0.0:
            if not symbolic:
                return np.zeros_like(discharge)
            else:
                # dH is set to zero in bounds
                return []

        if isinstance(is_disconnected, ca.MX) and not isinstance(big_m, float):
            raise ValueError("When `is_disconnected` is symbolic, `big_m` must be passed as well")
        if not symbolic and isinstance(is_disconnected, ca.MX):
            raise ValueError(
                "`is_disconnected` cannot be symbolic if the other dH/Q symbols are numeric"
            )

        if isinstance(is_disconnected, (float, int)) and is_disconnected == 1.0:
            if symbolic:
                # Pipe is always disconnected, so no head loss relationship needed
                return []
            else:
                # By definition we choose the head loss over disconnected
                # pipes to be zero.
                return 0.0

        if big_m is None:
            assert is_disconnected == 0.0
        else:
            assert big_m != 0.0

        wall_roughness = heat_network_options["wall_roughness"]
        if pipe_class is not None:
            diameter = pipe_class.inner_diameter
            area = pipe_class.area
            maximum_velocity = pipe_class.maximum_velocity
        else:
            diameter = parameters[f"{pipe}.diameter"]
            area = parameters[f"{pipe}.area"]
            maximum_velocity = network_settings["maximum_velocity"]

        # TODO: add commodity temperature to a gas network
        try:
            temperature = parameters[f"{pipe}.temperature"]
        except KeyError:
            temperature = 20.0

        try:
            has_control_valve = parameters[f"{pipe}.has_control_valve"]
        except KeyError:
            has_control_valve = False

        if head_loss_option == HeadLossOption.LINEAR:
            assert not has_control_valve

            ff = darcy_weisbach.friction_factor(
                maximum_velocity, diameter, wall_roughness, temperature
            )

            # Compute c_v constant (where |dH| ~ c_v * v^2)
            c_v = length * ff / (2 * GRAVITATIONAL_CONSTANT) / diameter

            linearization_velocity = maximum_velocity
            linearization_head_loss = c_v * linearization_velocity**2
            linearization_discharge = linearization_velocity * area

            expr = linearization_head_loss * discharge / linearization_discharge

            if symbolic:
                constraint_nominal = c_v * maximum_velocity**2
                # Interior point solvers, like IPOPT, do not like linearly dependent
                # tight inequality constraints. For this reason, we split the
                # constraints depending whether the Big-M formulation is used or not.
                if big_m is None:
                    return [((-1 * dh - expr) / constraint_nominal, 0.0, 0.0)]
                else:
                    constraint_nominal = (constraint_nominal * big_m) ** 0.5

                    return [
                        (
                            (-1 * dh - expr + is_disconnected * big_m) / constraint_nominal,
                            0.0,
                            np.inf,
                        ),
                        (
                            (-1 * dh - expr - is_disconnected * big_m) / constraint_nominal,
                            -np.inf,
                            0.0,
                        ),
                    ]
            else:
                return expr * np.sign(discharge)

        elif head_loss_option in {
            HeadLossOption.CQ2_INEQUALITY,
            HeadLossOption.CQ2_EQUALITY,
        }:
            ff = darcy_weisbach.friction_factor(
                heat_network_options["estimated_velocity"], diameter, wall_roughness, temperature
            )

            # Compute c_v constant (where |dH| ~ c_v * v^2)
            c_v = length * ff / (2 * GRAVITATIONAL_CONSTANT) / diameter

            v = discharge / area
            expr = c_v * v**2

            if symbolic:
                q_nominal = self.variable_nominal(f"{pipe}.Q")
                head_loss_nominal = self.variable_nominal(f"{pipe}.dH")
                constraint_nominal = (head_loss_nominal * c_v * (q_nominal / area) ** 2) ** 0.5

                if head_loss_option == HeadLossOption.CQ2_INEQUALITY:
                    ub = np.inf
                else:
                    ub = 0.0

                # Interior point solvers, like IPOPT, do not like linearly dependent
                # tight inequality constraints. For this reason, we split the
                # constraints depending whether the Big-M formulation is used or n
                if big_m is None:
                    equations = [((head_loss - expr) / constraint_nominal, 0.0, ub)]
                else:
                    constraint_nominal = (constraint_nominal * big_m) ** 0.5

                    equations = [
                        (
                            (head_loss - expr + is_disconnected * big_m) / constraint_nominal,
                            0.0,
                            np.inf,
                        )
                    ]
                    if head_loss_option == HeadLossOption.CQ2_EQUALITY:
                        equations.append(
                            (
                                (head_loss - expr - is_disconnected * big_m) / constraint_nominal,
                                -np.inf,
                                0.0,
                            ),
                        )
                return equations
            else:
                return expr

        elif (head_loss_option == HeadLossOption.LINEARIZED_DW
              or head_loss_option == HeadLossOption.LINEARIZED_N_LINES_EQUALITY
        ):
            n_linear_lines = network_settings["n_linearization_lines"]
            n_timesteps = len(optimization_problem.times())

            a, b = darcy_weisbach.get_linear_pipe_dh_vs_q_fit(
                diameter,
                length,
                wall_roughness,
                temperature=temperature,
                n_lines=n_linear_lines,
                v_max=maximum_velocity,
            )

            # The function above only gives result in the positive quadrant
            # (positive head loss, positive discharge). We also need a
            # positive head loss for _negative_ discharges.
            a = np.hstack([-a, a])
            b = np.hstack([b, b])

            # Vectorize constraint for speed
            if symbolic:
                q_nominal = optimization_problem.variable_nominal(f"{pipe}.Q")
                head_loss_nominal = optimization_problem.variable_nominal(f"{pipe}.dH")
                head_loss_vec = ca.repmat(head_loss, len(a))
                discharge_vec = ca.repmat(discharge, len(a))

                if isinstance(is_disconnected, ca.MX):
                    is_disconnected_vec = ca.repmat(is_disconnected, len(a))
                else:
                    # is_disconnected_vec = is_disconnected
                    is_disconnected_vec = np.ones((len(a) * discharge.size1(), 1), dtype=float) * is_disconnected

                a_vec = np.repeat(a, discharge.size1())
                b_vec = np.repeat(b, discharge.size1())

                constraint_nominal = np.abs(head_loss_nominal * a_vec * q_nominal) ** 0.5

                if big_m is None:
                    # We write the equation such that big_m is always used, even if
                    # it is None (i.e. not used). We do have to be sure to set it to 0,
                    # because we cannot multiple with "None".
                    big_m_lin = 0.0
                else:
                    big_m_lin = big_m
                    constraint_nominal = (constraint_nominal * big_m_lin) ** 0.5
                
                # add ons for multiple lines equality constraints -------------------

                constraints = []
                if head_loss_option == HeadLossOption.LINEARIZED_N_LINES_EQUALITY:
                    # pipe_linear_line_segment: will containt variables of negative and positive
                    # discharge possibilites for the pipe. This implies if a pipe is linearized with 2
                    # linear lines then pipe_linear_line_segment will have 2 * 2 variables
                    # Order of linear line variables:
                    #  - negative discharge line_1, line_2 ..._line_N
                    #  - positve discharge line_1, line_2 ..._line_N
                    pipe_linear_line_segment = self._pipe_linear_line_segment_map[pipe]
                    is_line_segment_active = []

                    for ii_line, ii_line_var in pipe_linear_line_segment.items():
                        # Create integer variable to activated/deactivate (1/0) a linear line segment
                        is_line_segment_active_var = optimization_problem.extra_variable(ii_line_var)

                        # Linear line segment activation variable for each time step of demand profile
                        is_line_segment_active.append(
                            ca.repmat(is_line_segment_active_var, n_timesteps)
                        )

                    # Calculate constraint to enforce that only 1 linear line segment can be active
                    # per time step for the current pipe
                    for itstep in range(n_timesteps):
                        is_line_segment_active_sum_per_timestep = 0.0
                        for ii_line in range(len(pipe_linear_line_segment)):

                            is_line_segment_active_sum_per_timestep = (
                                is_line_segment_active_sum_per_timestep
                                + is_line_segment_active[ii_line][itstep]
                            )
                        # Enforce only 1 line line segment to be active for per timestep for all
                        # timsteps
                        constraints.append(
                            (is_line_segment_active_sum_per_timestep, 1.0, 1.0),
                        )
                # Add weak inequality constraint, value >= 0.0 for all linear lines
                constraints.append(
                    (
                        (
                            head_loss_vec
                            - (a_vec * discharge_vec + b_vec)
                            + is_disconnected_vec * big_m_lin
                        )
                        / constraint_nominal,
                        0.0,
                        np.inf,
                    ),
                )
                # big_m_lin = big_m_lin * 2000000.0  # kvr ???? added this
                # big_m_lin = big_m_lin * 20000 # This was needed to get the 1 pipe gas network working when I specify maximum pipe press
                if head_loss_option == HeadLossOption.LINEARIZED_N_LINES_EQUALITY:
                    # Add equality constraint, value == 0.0 for all linear lines
                    # Loop twice due linear lines exsiting for positive and negative discharge
                    for ii in range(2):
                        for ii_line in range(n_linear_lines):
                            ii_start = (ii_line + 2 * ii) * n_timesteps
                            ii_end = ii_start + n_timesteps
                            constraints.append(
                                (
                                    (
                                        head_loss_vec[ii_start:ii_end]
                                        - (
                                            a_vec[ii_start:ii_end]
                                            * discharge_vec[ii_start:ii_end]
                                            + b_vec[ii_start:ii_end]
                                        )
                                        + is_disconnected_vec[ii_start:ii_end] * big_m_lin
                                        + big_m_lin * (1 - is_line_segment_active[ii_line][0:n_timesteps])
                                    )
                                    / constraint_nominal[ii_start:ii_end],
                                    0.0,
                                    np.inf,
                                ),
                            )
                    for ii in range(2):
                        for ii_line in range(n_linear_lines):
                            ii_start = (ii_line + 2 * ii) * n_timesteps
                            ii_end = ii_start + n_timesteps
                            constraints.append(
                                (
                                    (
                                        head_loss_vec[ii_start:ii_end]
                                        - (
                                            a_vec[ii_start:ii_end]
                                            * discharge_vec[ii_start:ii_end]
                                            + b_vec[ii_start:ii_end]
                                        )
                                        - is_disconnected_vec[ii_start:ii_end] * big_m_lin
                                        - big_m_lin * (1 - is_line_segment_active[ii_line][0:n_timesteps])
                                    )
                                    / constraint_nominal[ii_start:ii_end],
                                    -np.inf,
                                    0.0,
                                ),
                            )
                return constraints
            else:
                ret = np.amax(a * np.tile(discharge, (len(a), 1)).transpose() + b, axis=1)
                if isinstance(discharge, float):
                    ret = ret[0]
                return ret
            return []


    def _hydraulic_power(
        self,
        pipe: str,
        optimization_problem,
        heat_network_options,
        network_settings,
        parameters,
        discharge: Union[ca.MX, float, np.ndarray],
        hydraulic_power: Optional[Union[ca.MX, float, np.ndarray]] = None,
        dh: Optional[ca.MX] = None,
        is_disconnected: Union[ca.MX, int] = 0,
        big_m: Optional[float] = None,
        pipe_class: Optional[PipeClass] = None,
        flow_dir: Union[ca.MX, int] = 0,
    ) -> Union[List[Tuple[ca.MX, BT, BT]], float, np.ndarray]:
        """
        This function has two purposes:
        - return the hydraulic power constraint expression(s) or
        - compute the hydraulic power numerically (always positive).

        Note: the discharge value can be negative. In such a case the hydraulic_power in the
        constraint expression(s) will ensure that hydraulic_power is always positive. When a
        numerical value of hydraulic power is returned, it will always be a positive value, even
        when the discharge value is negative.

        Returning linearized hydraulic power or hydraulic power constraint expressions:
        - linearized hydraulic power (1 segment):
            = max hydraulic power * (discharge / maximum discharge)
            where hydraulic power:
                = delta pressure * discharge
                = rho * g * head loss * discharge
                = rho * g * (friction factor * length * velocity**2)/(diameter * 2 * g) * discharge
        - hydraulic power constraint expressions, n_number of linear line segments:
            = b + (a * discharge vector)
            where the discharge vector represents the volumetric flow rates for each segment

        `is_disconnected` can be used to specify whether a pipe is
        disconnected or not. This is most useful if a (boolean) ca.MX symbol
        is passed, which can then be used with a big-M formulation. The big-M
        itself then also needs to be passed via the `big_m` keyword argument.
        """

        if hydraulic_power is None:
            symbolic = False
        else:
            symbolic = True

        head_loss_option = network_settings["head_loss_option"]
        assert (
            head_loss_option != HeadLossOption.NO_HEADLOSS
        ), "This method should be skipped when NO_HEADLOSS is set."

        length = parameters[f"{pipe}.length"]

        if length == 0.0:
            if not symbolic:
                return np.zeros_like(discharge)
            else:
                return []

        if isinstance(is_disconnected, ca.MX) and not isinstance(big_m, float):
            raise ValueError("When `is_disconnected` is symbolic, `big_m` must be passed as well")
        if not symbolic and isinstance(is_disconnected, ca.MX):
            raise ValueError(
                "`is_disconnected` cannot be symbolic if the hydraulic_power symbol is numeric"
            )
        if isinstance(is_disconnected, (float, int)) and is_disconnected == 1.0:
            if symbolic:
                # Pipe is always disconnected, so no hydraulic power relationship needed
                return []
            else:
                # By definition we choose the hydraulic power over disconnected
                # pipes to be zero.
                return 0.0

        if big_m is None:
            assert is_disconnected == 0.0
        else:
            assert big_m != 0.0

        wall_roughness = heat_network_options["wall_roughness"]
        temperature = parameters[f"{pipe}.temperature"]
        rho = parameters[f"{pipe}.rho"]

        if pipe_class is not None:
            diameter = pipe_class.inner_diameter
            area = pipe_class.area
            maximum_velocity = pipe_class.maximum_velocity
        else:
            diameter = parameters[f"{pipe}.diameter"]
            area = parameters[f"{pipe}.area"]
            maximum_velocity = network_settings["maximum_velocity"]

        constraint_nominal = abs(
            parameters[f"{pipe}.rho"]
            * GRAVITATIONAL_CONSTANT
            * optimization_problem.variable_nominal(f"{pipe}.dH")
            * optimization_problem.variable_nominal(f"{pipe}.Q")
        )

        if head_loss_option == HeadLossOption.LINEAR:
            # Uitlized maximum_velocity instead of estimated_velocity (used in head loss linear
            # calc)
            ff = darcy_weisbach.friction_factor(
                maximum_velocity, diameter, wall_roughness, temperature
            )
            # Compute c_k constant (where |hydraulic power| ~ c_k * v^3)
            c_k = rho * ff * length * area / 2.0 / diameter
            # Compute linearized value
            max_hydraulic_power = c_k * maximum_velocity**3
            maximum_discharge = maximum_velocity * area
            hydraulic_power_linearized = max_hydraulic_power * discharge / maximum_discharge

            if symbolic:
                if big_m is None:
                    return [
                        (
                            (hydraulic_power - hydraulic_power_linearized) / constraint_nominal,
                            0.0,
                            0.0,
                        )
                    ]
                else:
                    constraint_nominal = (constraint_nominal * big_m) ** 0.5
                    # Add constraints to enforce hydraulic_power == hydraulic_power_linearized, by
                    # via the big_m method. The value of hydraulic_power must always be a positive
                    # value. Therefore, the flow direction is taken into account for the situation
                    # when the hydraulic_power_linearized is negative (hydraulic_power_linearized =
                    # f(discharge))
                    return [
                        (
                            (
                                hydraulic_power
                                - hydraulic_power_linearized
                                + (is_disconnected + (1.0 - flow_dir)) * big_m
                            )
                            / constraint_nominal,
                            0.0,
                            np.inf,
                        ),
                        (
                            (
                                hydraulic_power
                                - hydraulic_power_linearized
                                - (is_disconnected + (1.0 - flow_dir)) * big_m
                            )
                            / constraint_nominal,
                            -np.inf,
                            0.0,
                        ),
                        (
                            (
                                hydraulic_power
                                + hydraulic_power_linearized
                                + (is_disconnected + flow_dir) * big_m
                            )
                            / constraint_nominal,
                            0.0,
                            np.inf,
                        ),
                        (
                            (
                                hydraulic_power
                                + hydraulic_power_linearized
                                - (is_disconnected + flow_dir) * big_m
                            )
                            / constraint_nominal,
                            -np.inf,
                            0.0,
                        ),
                    ]
            else:
                return abs(hydraulic_power_linearized)

        elif head_loss_option == HeadLossOption.LINEARIZED_DW or HeadLossOption.LINEARIZED_N_LINES_EQUALITY:
            n_lines = network_settings["n_linearization_lines"]
            a_coef, b_coef = darcy_weisbach.get_linear_pipe_power_hydraulic_vs_q_fit(
                rho,
                diameter,
                length,
                wall_roughness,
                temperature=temperature,
                n_lines=n_lines,
                v_max=maximum_velocity,
            )
            discharge_vec = ca.repmat(discharge, len(a_coef))
            hydraulic_power_linearized_vec = a_coef * discharge_vec + b_coef

            if symbolic:
                hydraulic_power_vec = ca.repmat(hydraulic_power, len(a_coef))

                if isinstance(is_disconnected, ca.MX):
                    is_disconnected_vec = ca.repmat(is_disconnected, len(a_coef))
                else:
                    is_disconnected_vec = is_disconnected

                if big_m is None:
                    # We write the equation such that big_m is always used, even if
                    # it is None (i.e. not used). We do have to be sure to set it to 0,
                    # because we cannot multiple with "None".
                    big_m_lin = 0.0
                else:
                    big_m_lin = big_m
                    constraint_nominal = (constraint_nominal * big_m_lin) ** 0.5
                    # Add constraints to enforce hydraulic_power_vec >=
                    # hydraulic_power_linearized_vec. The value of hydraulic_power_vec must always
                    # be a positive value. Therefore, the flow direction is taken into account for
                    # the situation when the hydraulic_power_linearized_vec is negative
                    # (hydraulic_power_linearized_vec = f(discharge))
                return [
                    (
                        (
                            hydraulic_power_vec
                            - hydraulic_power_linearized_vec
                            + (is_disconnected_vec + (1.0 - flow_dir)) * big_m_lin
                        )
                        / constraint_nominal,
                        0.0,
                        np.inf,
                    ),
                    (
                        (
                            hydraulic_power_vec
                            + hydraulic_power_linearized_vec
                            + (is_disconnected_vec + flow_dir) * big_m_lin
                        )
                        / constraint_nominal,
                        0.0,
                        np.inf,
                    ),
                ]
            else:
                # Calc the max hydraulic power out of all the linear sections
                max_hydraulic_power_linearized = np.amax(hydraulic_power_linearized_vec, axis=1)
                if isinstance(discharge, float):
                    max_hydraulic_power_linearized = max_hydraulic_power_linearized[0]
                return abs(max_hydraulic_power_linearized)
        else:
            assert (
                head_loss_option == HeadLossOption.LINEARIZED_DW
                or head_loss_option == HeadLossOption.LINEAR or head_loss_option == HeadLossOption.LINEARIZED_N_LINES_EQUALITY
            ), "This method only caters for head_loss_option: LINEAR & LINEARIZED_DW."

    def _pipe_head_loss_path_constraints(self, optimization_problem, _ensemble_member):
        """
        We set this constraint relating .dH to the upstream and downstream
        heads here in the Mixin for scaling purposes (dH nominal is
        calculated in pre()).
        """
        constraints = []

        pipe_type = "pipe"
        commodity = "Heat"
        if len(optimization_problem.heat_network_components.get(pipe_type, [])) == 0:
            pipe_type = "gas_pipe"
            commodity = NetworkSettings.NETWORK_TYPE_GAS

        for pipe in optimization_problem.heat_network_components.get(pipe_type, []):
            dh = optimization_problem.state(f"{pipe}.dH")
            h_down = optimization_problem.state(f"{pipe}.{commodity}Out.H")
            h_up = optimization_problem.state(f"{pipe}.{commodity}In.H")

            constraint_nominal = (
                optimization_problem.variable_nominal(f"{pipe}.dH")
                * optimization_problem.variable_nominal(f"{pipe}.{commodity}In.H")
            ) ** 0.5
            constraints.append(((dh - (h_down - h_up)) / constraint_nominal, 0.0, 0.0))

        return constraints

    def _demand_head_loss_path_constraints(self, optimization_problem, _ensemble_member):
        """
        This function adds constraints for a minimum pressure drop at demands. This minimum
        pressure drop is often required in practice to guarantee that it is possible to increase
        the flow rate at that specific demand, if needed, by opening the control valve.
        """
        constraints = []

        options = optimization_problem.heat_network_options()
        components = optimization_problem.heat_network_components

        # Convert minimum pressure at far point from bar to meter (water) head
        min_head_loss = options["minimum_pressure_far_point"] * 10.2

        for d in components.get("demand", []):
            constraints.append(
                (
                    optimization_problem.state(f"{d}.HeatIn.H")
                    - optimization_problem.state(f"{d}.HeatOut.H"),
                    min_head_loss,
                    np.inf,
                )
            )

        return constraints
