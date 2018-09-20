/* Copyright 2017-2018 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * MAAS Pod Details Controller
 */

angular.module('MAAS').controller('PodDetailsController', [
    '$scope', '$rootScope', '$location', '$routeParams',
    'PodsManager', 'GeneralManager', 'UsersManager', 'DomainsManager',
    'ZonesManager', 'MachinesManager', 'ManagerHelperService', 'ErrorService',
    'ResourcePoolsManager', 'ValidationService',
    function(
        $scope, $rootScope, $location, $routeParams,
        PodsManager, GeneralManager, UsersManager, DomainsManager,
        ZonesManager, MachinesManager, ManagerHelperService, ErrorService,
        ResourcePoolsManager, ValidationService) {

        // Set title and page.
        $rootScope.title = "Loading...";
        $rootScope.page = "pods";

        // Initial values.
        $scope.loaded = false;
        $scope.pod = null;
        $scope.podManager = PodsManager;
        $scope.action = {
          option: null,
          options: [
            {
              name: 'refresh',
              title: 'Refresh',
              sentence: 'refresh',
              operation: angular.bind(PodsManager, PodsManager.refresh)
            },
            {
              name: 'delete',
              title: 'Delete',
              sentence: 'delete',
              operation: angular.bind(PodsManager, PodsManager.deleteItem)
            }
          ],
          inProgress: false,
          error: null
        };
        $scope.compose = {
          action: {
            name: 'compose',
            title: 'Compose',
            sentence: 'compose'
          },
          obj: {
            storage: [{
              type: 'local',
              size: 8,
              tags: [],
              boot: true
            }]
          }
        };
        $scope.power_types = GeneralManager.getData("power_types");
        $scope.domains = DomainsManager.getItems();
        $scope.zones = ZonesManager.getItems();
        $scope.pools = ResourcePoolsManager.getItems();
        $scope.section = {
          area: 'summary'
        };
        $scope.machinesSearch = 'pod-id:=invalid';
        $scope.editing = false;

        // Pod name section.
        $scope.name = {
            editing: false,
            value: "",
        };

        // Return true if the authenticated user is super user.
        $scope.isSuperUser = function() {
            return UsersManager.isSuperUser();
        };

        // Return true if at least a rack controller is connected to the
        // region controller.
        $scope.isRackControllerConnected = function() {
            // If power_types exist then a rack controller is connected.
            return $scope.power_types.length > 0;
        };

        // Return true when the edit buttons can be clicked.
        $scope.canEdit = function() {
            return (
                $scope.isRackControllerConnected() &&
                    $scope.isSuperUser());
        };

        // Called to edit the pod configuration.
        $scope.editPodConfiguration = function() {
            if(!$scope.canEdit()) {
                return;
            }
            $scope.editing = true;
        };

        // Called when the cancel or save button is pressed.
        $scope.exitEditPodConfiguration = function() {
            $scope.editing = false;
        };

        // Called to edit the pod name.
        $scope.editName = function() {
            if(!$scope.canEdit()) {
                return;
            }

            // Do nothing if already editing because we don't
            // want to reset the current value.
            if($scope.name.editing) {
                return;
            }
            $scope.name.editing = true;
            $scope.name.value = $scope.pod.name;
        };

        // Return true when the pod name is invalid.
        $scope.editNameInvalid = function() {
            // Not invalid unless editing.
            if(!$scope.name.editing) {
                return false;
            }

            // The value cannot be blank.
            var value = $scope.name.value;
            if(value.length === 0) {
                return true;
            }
            return !ValidationService.validateHostname(value);
        };


        // Called to cancel editing of the pod name.
        $scope.cancelEditName = function() {
            $scope.name.editing = false;
            updateName();
        };

        // Called to save editing of pod name.
        $scope.saveEditName = function() {
            // Does nothing if invalid.
            if($scope.editNameInvalid()) {
                return;
            }
            $scope.name.editing = false;

            // Copy the pod and make the changes.
            var pod = angular.copy($scope.pod);
            pod.name = $scope.name.value;

            // Update the pod.
            $scope.updatePod(pod);
        };

        function updateName() {
            // Don't update the value if in editing mode.
            // As this would overwrite the users changes.
            if($scope.name.editing) {
                return;
            }
            $scope.name.value = $scope.pod.name;
        }

        // Update the pod with new data on the region.
        $scope.updatePod = function(pod) {
            return $scope.podManager.updateItem(pod).then(function(pod) {
                updateName();
            }, function(error) {
                console.log(error);
                updateName();
            });
        };

        // Return true if there is an action error.
        $scope.isActionError = function() {
            return $scope.action.error !== null;
        };

        // Called when the action.option has changed.
        $scope.actionOptionChanged = function() {
            // Clear the action error.
            $scope.action.error = null;
        };

        // Cancel the action.
        $scope.actionCancel = function() {
            $scope.action.option = null;
            $scope.action.error = null;
        };

        // Perform the action.
        $scope.actionGo = function() {
            $scope.action.inProgress = true;
            $scope.action.option.operation($scope.pod).then(function() {
                  // If the action was delete, then go back to listing.
                  if($scope.action.option.name === "delete") {
                      $location.path("/pods");
                  }
                  $scope.action.inProgress = false;
                  $scope.action.option = null;
                  $scope.action.error = null;
              }, function(error) {
                  $scope.action.inProgress = false;
                  $scope.action.error = error;
              });
        };

        // Return the title of the pod type.
        $scope.getPodTypeTitle = function() {
            var i;
            for(i = 0; i < $scope.power_types.length; i++) {
                var power_type = $scope.power_types[i];
                if(power_type.name === $scope.pod.type) {
                    return power_type.description;
                }
            }
            return $scope.pod.type;
        };

        // Returns true if the pod is composable.
        $scope.canCompose = function() {
            if(angular.isObject($scope.pod)) {
                return ($scope.isSuperUser() &&
                    $scope.pod.capabilities.indexOf('composable') >= 0);
            } else {
                return false;
            }
        };

        // Opens the compose action menu.
        $scope.composeMachine = function() {
            $scope.action.option = $scope.compose.action;
        };

        // Called before the compose params is sent over the websocket.
        $scope.composePreProcess = function(params) {
            params = angular.copy(params);
            params.id = $scope.pod.id;
            // Sort boot disk first.
            var sorted = $scope.compose.obj.storage.sort(function(a, b) {
              if(a.boot === b.boot) {
                return 0;
              } else if(a.boot && !b.boot) {
                return -1;
              } else {
                return 1;
              }
            });
            // Create the storage constraint.
            var storage = [];
            angular.forEach(sorted, function(disk, idx) {
              var constraint = idx + ':' + disk.size;
              var tags = disk.tags.map(function(tag) {
                return tag.text;
              });
              tags.splice(0, 0, disk.type);
              constraint += '(' + tags.join(',') + ')';
              storage.push(constraint);
            });
            params.storage = storage.join(',');
            return params;
        };

        $scope.copyToClipboard = function($event) {
            var clipboardParent = $event.currentTarget.previousSibling;
            var clipboardValue = clipboardParent.previousSibling.value;
            var el = document.createElement('textarea');
            el.value = clipboardValue;
            document.body.appendChild(el);
            el.select();
            document.execCommand('copy');
            document.body.removeChild(el);
        };

        // Called to cancel composition.
        $scope.cancelCompose = function() {
          $scope.compose.obj = {
            storage: [{
              type: 'local',
              size: 8,
              tags: [],
              boot: true
            }]
          };
          $scope.action.option = null;
        };

        // Add another storage device.
        $scope.composeAddStorage = function() {
          var storage = {
            type: 'local',
            size: 8,
            tags: [],
            boot: false
          };
          if($scope.pod.capabilities.indexOf('iscsi_storage') >= 0) {
            storage.type = 'iscsi';
          }
          $scope.compose.obj.storage.push(storage);
        };

        // Change which disk is the boot disk.
        $scope.composeSetBootDisk = function(storage) {
          angular.forEach($scope.compose.obj.storage, function(disk) {
            disk.boot = false;
          });
          storage.boot = true;
        };

        // Remove a disk from storage config.
        $scope.composeRemoveDisk = function(storage) {
          var idx = $scope.compose.obj.storage.indexOf(storage);
          if(idx >= 0) {
            $scope.compose.obj.storage.splice(idx, 1);
          }
        };

        // Start watching key fields.
        $scope.startWatching = function() {
            $scope.$watch("pod.name", function() {
                $rootScope.title = 'Pod ' + $scope.pod.name;
                updateName();
            });
            $scope.$watch("pod.capabilities", function() {
                // Show the composable action if the pod supports composition.
                var idx = $scope.action.options.indexOf(
                    $scope.compose.action);
                if(!$scope.canCompose()) {
                    if(idx >= 0) {
                        $scope.action.options.splice(idx, 1);
                    }
                } else {
                    if(idx === -1) {
                        $scope.action.options.splice(
                            0, 0, $scope.compose.action);
                    }
                }
            });
            $scope.$watch("action.option", function(now, then) {
                // When the compose action is selected set the default
                // parameters.
                if(now && now.name === 'compose') {
                    if(!then || then.name !== 'compose') {
                        $scope.compose.obj.domain = (
                            DomainsManager.getDefaultDomain().id);
                        $scope.compose.obj.zone = (
                            ZonesManager.getDefaultZone().id);
                        $scope.compose.obj.pool = $scope.pod.default_pool;
                    }
                }
            });
        };

        // Load all the required managers.
        ManagerHelperService.loadManagers($scope, [
            PodsManager, GeneralManager, UsersManager,
            DomainsManager, ZonesManager, MachinesManager,
            ResourcePoolsManager]).then(function() {

            // Possibly redirected from another controller that already had
            // this pod set to active. Only call setActiveItem if not already
            // the activeItem.
            var activePod = PodsManager.getActiveItem();
            if(angular.isObject(activePod) &&
                activePod.id === parseInt($routeParams.id, 10)) {
                $scope.pod = activePod;
                $scope.loaded = true;
                $scope.machinesSearch = 'pod-id:=' + $scope.pod.id;
                $scope.startWatching();
            } else {
                PodsManager.setActiveItem(
                    parseInt($routeParams.id, 10)).then(function(pod) {
                        $scope.pod = pod;
                        $scope.loaded = true;
                        $scope.machinesSearch = 'pod-id:=' + $scope.pod.id;
                        $scope.startWatching();
                    }, function(error) {
                        ErrorService.raiseError(error);
                    });
            }
        });
    }]);