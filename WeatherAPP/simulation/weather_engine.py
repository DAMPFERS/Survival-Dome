from simulation.wind_field import WindField
from simulation.dust_system import DustStorm

from core.config import *


class WeatherEngine:

    def __init__(self):

        self.wind_field = WindField(
            WINDOW_WIDTH,
            WINDOW_HEIGHT
        )

        self.dust_storms = [

            DustStorm(
                500,
                400,
                140
            ),

            DustStorm(
                1200,
                650,
                180
            )
        ]

    def update(self, delta_time):

        self.wind_field.update(delta_time)

        for storm in self.dust_storms:

            storm.update(
                delta_time,
                self.wind_field
            )