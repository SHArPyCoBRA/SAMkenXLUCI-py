# Generated by the gRPC Python protocol compiler plugin. DO NOT EDIT!
import grpc
from grpc.framework.common import cardinality
from grpc.framework.interfaces.face import utilities as face_utilities

import reservation_manager_pb2 as reservation__manager__pb2


class ReservationManagerStub(object):

  def __init__(self, channel):
    """Constructor.

    Args:
      channel: A grpc.Channel.
    """
    self.Poll = channel.unary_unary(
        '/google.internal.devtools.workerfarm.v1test1.ReservationManager/Poll',
        request_serializer=reservation__manager__pb2.PollRequest.SerializeToString,
        response_deserializer=reservation__manager__pb2.PollResponse.FromString,
        )
    self.CreateReservation = channel.unary_unary(
        '/google.internal.devtools.workerfarm.v1test1.ReservationManager/CreateReservation',
        request_serializer=reservation__manager__pb2.CreateReservationRequest.SerializeToString,
        response_deserializer=reservation__manager__pb2.Reservation.FromString,
        )
    self.CancelReservation = channel.unary_unary(
        '/google.internal.devtools.workerfarm.v1test1.ReservationManager/CancelReservation',
        request_serializer=reservation__manager__pb2.CancelReservationRequest.SerializeToString,
        response_deserializer=reservation__manager__pb2.CancelReservationResponse.FromString,
        )


class ReservationManagerServicer(object):

  def Poll(self, request, context):
    context.set_code(grpc.StatusCode.UNIMPLEMENTED)
    context.set_details('Method not implemented!')
    raise NotImplementedError('Method not implemented!')

  def CreateReservation(self, request, context):
    context.set_code(grpc.StatusCode.UNIMPLEMENTED)
    context.set_details('Method not implemented!')
    raise NotImplementedError('Method not implemented!')

  def CancelReservation(self, request, context):
    context.set_code(grpc.StatusCode.UNIMPLEMENTED)
    context.set_details('Method not implemented!')
    raise NotImplementedError('Method not implemented!')


def add_ReservationManagerServicer_to_server(servicer, server):
  rpc_method_handlers = {
      'Poll': grpc.unary_unary_rpc_method_handler(
          servicer.Poll,
          request_deserializer=reservation__manager__pb2.PollRequest.FromString,
          response_serializer=reservation__manager__pb2.PollResponse.SerializeToString,
      ),
      'CreateReservation': grpc.unary_unary_rpc_method_handler(
          servicer.CreateReservation,
          request_deserializer=reservation__manager__pb2.CreateReservationRequest.FromString,
          response_serializer=reservation__manager__pb2.Reservation.SerializeToString,
      ),
      'CancelReservation': grpc.unary_unary_rpc_method_handler(
          servicer.CancelReservation,
          request_deserializer=reservation__manager__pb2.CancelReservationRequest.FromString,
          response_serializer=reservation__manager__pb2.CancelReservationResponse.SerializeToString,
      ),
  }
  generic_handler = grpc.method_handlers_generic_handler(
      'google.internal.devtools.workerfarm.v1test1.ReservationManager', rpc_method_handlers)
  server.add_generic_rpc_handlers((generic_handler,))
